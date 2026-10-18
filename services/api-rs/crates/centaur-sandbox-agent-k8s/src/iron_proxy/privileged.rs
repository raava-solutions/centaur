//! Approver-gated privileged sandbox access: per-sandbox credentials + egress.
//!
//! The session runtime decides which sandboxes are gated. This module only
//! materializes that decision for the Kubernetes backend: it installs the
//! credential files into the RUNNING sandbox pod (warm spares stay untouched
//! until the bind that claims them) and ensures the single per-sandbox egress
//! allowance that goes with them. Files and destinations travel as one payload,
//! so a sandbox can never hold the credentials without their egress, or the
//! reverse.
//!
//! Secret bytes only ever move over the exec stdin pipe: they never appear in
//! command lines, ConfigMaps, Secrets, logs, or environment dumps.

use std::collections::BTreeMap;

use k8s_openapi::api::networking::v1::{
    IPBlock, NetworkPolicy, NetworkPolicyPeer, NetworkPolicySpec,
};
use kube::api::{AttachParams, DeleteParams, PostParams};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use centaur_sandbox_core::{
    PrivilegedSandboxAccess, PrivilegedSandboxFile, SANDBOX_PRIVILEGED_ACCESS_DIR,
    SANDBOX_SSH_CONFIG_PATH, SandboxError, SandboxId, SandboxResult,
};

use crate::AgentSandboxBackend;

/// Label carried only by the privileged-access egress NetworkPolicy, so the
/// grant is identifiable independently of the sandbox egress policy.
const PRIVILEGED_ACCESS_LABEL: &str = "centaur.ai/privileged-access";

/// Name of the fingerprint file inside [`SANDBOX_PRIVILEGED_ACCESS_DIR`] that
/// records the installed payload, making reinstall idempotent.
const FINGERPRINT_FILE: &str = ".payload-fingerprint";

impl AgentSandboxBackend {
    /// Install the privileged-access payload into a running sandbox pod and
    /// ensure its egress allowance exists. Idempotent: an identical payload
    /// leaves the pod untouched and only re-verifies the policy.
    pub(crate) async fn install_privileged_access_resources(
        &self,
        id: &SandboxId,
        access: &PrivilegedSandboxAccess,
    ) -> SandboxResult<()> {
        validate_access_files(&access.files)?;
        let fingerprint = access_fingerprint(access);
        if self.read_installed_fingerprint(id).await? != Some(fingerprint.clone()) {
            for file in &access.files {
                self.exec_install_file(id, file).await?;
            }
            self.exec_install_file(
                id,
                &PrivilegedSandboxFile {
                    path: fingerprint_path(),
                    mode: 0o600,
                    contents: fingerprint.into_bytes(),
                },
            )
            .await?;
        }
        self.ensure_privileged_access_policy(id, access).await
    }

    /// Best-effort removal of the privileged-access payload and its egress
    /// allowance. A missing pod or policy is success: pod teardown already
    /// destroys the installed files, and the goal state is "no privileged
    /// access".
    pub(crate) async fn clear_privileged_access_resources(
        &self,
        id: &SandboxId,
    ) -> SandboxResult<()> {
        let command =
            format!("rm -rf {SANDBOX_PRIVILEGED_ACCESS_DIR} && rm -f {SANDBOX_SSH_CONFIG_PATH}");
        if let Err(error) = self.exec_sandbox_command(id, &command, None).await {
            tracing::debug!(
                sandbox_id = id.as_str(),
                %error,
                "privileged access file removal skipped (sandbox likely gone)"
            );
        }
        self.delete_privileged_access_policy(id).await
    }

    /// Read the installed payload fingerprint, or `None` when the payload is
    /// absent or unreadable. An unreadable marker must trigger a full
    /// reinstall, never a skip.
    async fn read_installed_fingerprint(&self, id: &SandboxId) -> SandboxResult<Option<String>> {
        let command = format!("cat {} 2>/dev/null || true", fingerprint_path());
        let stdout = match self.exec_sandbox_command(id, &command, None).await {
            Ok(stdout) => stdout,
            Err(error) => {
                tracing::debug!(
                    sandbox_id = id.as_str(),
                    %error,
                    "privileged access fingerprint unreadable; reinstalling payload"
                );
                return Ok(None);
            }
        };
        Ok(Some(String::from_utf8_lossy(&stdout).trim().to_owned()))
    }

    /// Write one payload file into the sandbox pod via exec stdin, so the
    /// bytes never land in the command line, ConfigMaps, Secrets, or logs.
    async fn exec_install_file(
        &self,
        id: &SandboxId,
        file: &PrivilegedSandboxFile,
    ) -> SandboxResult<()> {
        let parent = parent_directory(&file.path);
        let command = format!(
            "mkdir -p {parent} && cat > {path} && chmod {mode:o} {path}",
            path = file.path,
            mode = file.mode,
        );
        self.exec_sandbox_command(id, &command, Some(&file.contents))
            .await?;
        Ok(())
    }

    /// Ensure the per-sandbox privileged egress policy exists with exactly the
    /// payload's destinations. NetworkPolicies union, so this adds the
    /// destinations on top of the sandbox's existing egress policy without
    /// touching it; sandboxes without the policy keep today's egress.
    async fn ensure_privileged_access_policy(
        &self,
        id: &SandboxId,
        access: &PrivilegedSandboxAccess,
    ) -> SandboxResult<()> {
        let policy = privileged_access_network_policy(id, access);
        let name = privileged_access_policy_name(id);
        let policies = self.network_policies();
        match policies
            .replace(&name, &PostParams::default(), &policy)
            .await
        {
            Ok(_) => Ok(()),
            Err(err) if crate::is_not_found(&err) => policies
                .create(&PostParams::default(), &policy)
                .await
                .map_err(|err| {
                    crate::map_kube_error("create privileged access network policy", err)
                })
                .map(|_| ()),
            Err(err) => Err(crate::map_kube_error(
                "replace privileged access network policy",
                err,
            )),
        }
    }

    /// Delete the per-sandbox privileged egress policy; a missing policy is
    /// success.
    pub(crate) async fn delete_privileged_access_policy(
        &self,
        id: &SandboxId,
    ) -> SandboxResult<()> {
        self.network_policies()
            .delete(&privileged_access_policy_name(id), &DeleteParams::default())
            .await
            .map(|_| ())
            .or_else(|err| {
                if crate::is_not_found(&err) {
                    Ok(())
                } else {
                    Err(crate::map_kube_error(
                        "delete privileged access network policy",
                        err,
                    ))
                }
            })
    }

    /// Run `sh -c <command>` in the sandbox workload container, optionally
    /// writing `stdin` bytes and returning stdout. The terminal exit status is
    /// checked, so a failed install never looks successful.
    async fn exec_sandbox_command(
        &self,
        id: &SandboxId,
        command: &str,
        stdin: Option<&[u8]>,
    ) -> SandboxResult<Vec<u8>> {
        let mut params = AttachParams::default()
            .stdout(true)
            .stderr(false)
            .container(self.config.container_name.as_str());
        if stdin.is_some() {
            params = params.stdin(true);
        }
        let mut process = self
            .pods()
            .exec(id.as_str(), vec!["sh", "-c", command], &params)
            .await
            .map_err(|err| crate::map_kube_error("exec sandbox privileged access command", err))?;
        let status = process.take_status();
        if let Some(bytes) = stdin {
            let mut writer = process
                .stdin()
                .ok_or_else(|| SandboxError::io("exec stdin pipe was not opened"))?;
            writer
                .write_all(bytes)
                .await
                .map_err(|err| SandboxError::io(format!("write exec stdin: {err}")))?;
            writer
                .shutdown()
                .await
                .map_err(|err| SandboxError::io(format!("close exec stdin: {err}")))?;
        }
        let mut stdout = Vec::new();
        if let Some(mut reader) = process.stdout() {
            reader
                .read_to_end(&mut stdout)
                .await
                .map_err(|err| SandboxError::io(format!("read exec stdout: {err}")))?;
        }
        process
            .join()
            .await
            .map_err(|err| SandboxError::io(format!("sandbox exec did not finish: {err}")))?;
        match status {
            Some(status) => match status.await {
                Some(status)
                    if status
                        .status
                        .as_deref()
                        .is_some_and(|value| value.eq_ignore_ascii_case("success")) => {}
                Some(status) => {
                    return Err(SandboxError::io(format!(
                        "sandbox exec failed: {}",
                        status
                            .message
                            .unwrap_or_else(|| "unknown exec failure".to_owned())
                    )));
                }
                None => return Err(SandboxError::io("sandbox exec returned no exit status")),
            },
            None => return Err(SandboxError::io("sandbox exec status channel unavailable")),
        }
        Ok(stdout)
    }
}

fn privileged_access_policy_name(id: &SandboxId) -> String {
    format!("{}-privileged-access", id.as_str())
}

fn privileged_access_labels(id: &SandboxId) -> BTreeMap<String, String> {
    let mut labels = super::sandbox_labels(id);
    labels.insert(PRIVILEGED_ACCESS_LABEL.to_owned(), "true".to_owned());
    labels
}

fn privileged_access_network_policy(
    id: &SandboxId,
    access: &PrivilegedSandboxAccess,
) -> NetworkPolicy {
    let peers: Vec<NetworkPolicyPeer> = access
        .egress_cidrs
        .iter()
        .map(|cidr| NetworkPolicyPeer {
            ip_block: Some(IPBlock {
                cidr: cidr.clone(),
                except: None,
            }),
            ..Default::default()
        })
        .collect();
    NetworkPolicy {
        metadata: super::object_meta(
            privileged_access_policy_name(id),
            privileged_access_labels(id),
        ),
        spec: Some(NetworkPolicySpec {
            pod_selector: Some(super::label_selector(super::sandbox_labels(id))),
            ingress: None,
            policy_types: Some(vec!["Egress".to_owned()]),
            egress: Some(vec![super::egress_to(
                peers,
                vec![super::network_port(access.egress_port)],
            )]),
        }),
    }
}

fn fingerprint_path() -> String {
    format!("{SANDBOX_PRIVILEGED_ACCESS_DIR}/{FINGERPRINT_FILE}")
}

/// Payload files must live under the privileged access directory (the managed
/// ssh client config is the one exception), and every path must be safe to
/// embed in an `sh -c` command. Anything else fails the install closed.
fn validate_access_files(files: &[PrivilegedSandboxFile]) -> SandboxResult<()> {
    for file in files {
        let path = file.path.as_str();
        let in_payload_dir = path
            .strip_prefix(SANDBOX_PRIVILEGED_ACCESS_DIR)
            .is_some_and(|rest| rest.starts_with('/'));
        // No `..`/`.` components anywhere: a prefix check alone would accept
        // traversal out of the managed directory, and these paths are embedded
        // verbatim into an `sh -c` command.
        let components_settled = path
            .split('/')
            .all(|component| component != ".." && component != ".");
        let allowed = (in_payload_dir || path == SANDBOX_SSH_CONFIG_PATH)
            && components_settled
            && path_is_shell_safe(path)
            && !path.ends_with('/');
        if !allowed {
            return Err(SandboxError::io(format!(
                "privileged access file path must be shell-safe and under {SANDBOX_PRIVILEGED_ACCESS_DIR} (or {SANDBOX_SSH_CONFIG_PATH}): {path}"
            )));
        }
    }
    Ok(())
}

fn path_is_shell_safe(path: &str) -> bool {
    path.bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'/'))
}

fn parent_directory(path: &str) -> &str {
    match path.rfind('/') {
        Some(0) | None => "/",
        Some(index) => &path[..index],
    }
}

/// Deterministic digest of the whole payload (files, modes, destinations).
/// The marker file records it inside the sandbox, so an unchanged payload
/// skips reinstallation while any rotation reinstalls.
fn access_fingerprint(access: &PrivilegedSandboxAccess) -> String {
    let mut hasher = Sha256::new();
    let mut files = access.files.clone();
    files.sort_by(|left, right| left.path.cmp(&right.path));
    for file in &files {
        hasher.update(file.path.as_bytes());
        hasher.update([0]);
        hasher.update(file.mode.to_be_bytes());
        hasher.update([0]);
        hasher.update(&file.contents);
        hasher.update([0]);
    }
    let mut cidrs = access.egress_cidrs.clone();
    cidrs.sort();
    for cidr in &cidrs {
        hasher.update(cidr.as_bytes());
        hasher.update([0]);
    }
    hasher.update(access.egress_port.to_be_bytes());
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    use k8s_openapi::apimachinery::pkg::util::intstr::IntOrString;

    fn file(path: &str, contents: &[u8]) -> PrivilegedSandboxFile {
        PrivilegedSandboxFile {
            path: path.to_owned(),
            mode: 0o600,
            contents: contents.to_vec(),
        }
    }

    fn access() -> PrivilegedSandboxAccess {
        PrivilegedSandboxAccess {
            files: vec![
                file(
                    &format!("{SANDBOX_PRIVILEGED_ACCESS_DIR}/id_ed25519"),
                    b"key-bytes",
                ),
                file(
                    &format!("{SANDBOX_PRIVILEGED_ACCESS_DIR}/known_hosts"),
                    b"pinned-host-keys",
                ),
                file(SANDBOX_SSH_CONFIG_PATH, b"include-stub"),
            ],
            egress_cidrs: vec![
                "10.200.160.152/32".to_owned(),
                "10.200.160.153/32".to_owned(),
            ],
            egress_port: 22,
        }
    }

    #[test]
    fn privileged_access_policy_selects_sandbox_pod_and_limits_destinations() {
        let id = SandboxId::new("asbx-1792085681765-2");
        let policy = privileged_access_network_policy(&id, &access());

        assert_eq!(
            policy.metadata.name.as_deref(),
            Some("asbx-1792085681765-2-privileged-access")
        );
        assert_eq!(
            policy
                .metadata
                .labels
                .as_ref()
                .unwrap()
                .get(PRIVILEGED_ACCESS_LABEL),
            Some(&"true".to_owned())
        );

        let spec = policy.spec.expect("policy spec");
        let pod_selector = spec.pod_selector.expect("pod selector");
        assert!(!pod_selector.match_labels.expect("match labels").is_empty());
        assert_eq!(
            spec.policy_types.as_deref(),
            Some(["Egress".to_owned()].as_slice()),
            "the grant is additive egress only"
        );

        let rules = spec.egress.expect("egress rules");
        assert_eq!(rules.len(), 1);
        let peers = rules[0].to.as_ref().expect("egress peers");
        let cidrs: Vec<&str> = peers
            .iter()
            .map(|peer| peer.ip_block.as_ref().expect("ip block").cidr.as_str())
            .collect();
        assert_eq!(cidrs, ["10.200.160.152/32", "10.200.160.153/32"]);
        let ports = rules[0].ports.as_ref().expect("egress ports");
        assert_eq!(ports.len(), 1);
        assert!(
            matches!(ports[0].port.as_ref(), Some(IntOrString::Int(port)) if *port == 22),
            "the port is the configured numeric privileged port"
        );
    }

    #[test]
    fn validate_accepts_payload_and_ssh_config_paths() {
        assert!(validate_access_files(&access().files).is_ok());
    }

    #[test]
    fn validate_rejects_paths_outside_the_payload_directory() {
        assert!(validate_access_files(&[file("/home/agent/.ssh/id_ed25519", b"key")]).is_err());
        assert!(
            validate_access_files(&[file(
                "/home/agent/.ssh/centaur-privileged/../id_ed25519",
                b"key"
            )])
            .is_err()
        );
        assert!(
            validate_access_files(&[file(
                &format!("{SANDBOX_PRIVILEGED_ACCESS_DIR}/x; rm -rf /"),
                b"key"
            )])
            .is_err()
        );
        assert!(
            validate_access_files(&[file(
                &format!("{SANDBOX_PRIVILEGED_ACCESS_DIR}/a b"),
                b"key"
            )])
            .is_err()
        );
    }

    #[test]
    fn fingerprint_is_stable_order_insensitive_and_covers_rotation() {
        let base = access();
        assert_eq!(access_fingerprint(&base), access_fingerprint(&base.clone()));

        let mut reordered = base.clone();
        reordered.files.reverse();
        reordered.egress_cidrs.reverse();
        assert_eq!(access_fingerprint(&base), access_fingerprint(&reordered));

        let mut rotated_key = base.clone();
        rotated_key.files[0].contents = b"rotated-key-bytes".to_vec();
        assert_ne!(access_fingerprint(&base), access_fingerprint(&rotated_key));

        let mut narrower_egress = base;
        narrower_egress.egress_cidrs.pop();
        assert_ne!(
            access_fingerprint(&access()),
            access_fingerprint(&narrower_egress)
        );
    }
}
