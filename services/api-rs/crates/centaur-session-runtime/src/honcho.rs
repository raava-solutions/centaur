//! Honcho peer-memory integration.
//!
//! When enabled, the control plane mirrors every session turn into a Honcho
//! workspace (user messages on append, the final agent answer on execution
//! completion) and prepends the requester's derived Honcho context to each
//! executed user turn. Every call is best-effort: Honcho being down or slow
//! must never fail or block a turn, so all errors are logged and dropped by
//! the caller and the injection read runs under a short timeout.
//!
//! Configuration (all via environment, read once at startup):
//! - `HONCHO_ENABLED`: truthy (`1`, `true`, `yes`, `on`) enables the integration.
//! - `HONCHO_BASE_URL`: Honcho API base URL (required when enabled).
//! - `HONCHO_WORKSPACE`: workspace id (default `centaur`).
//! - `HONCHO_API_KEY`: optional bearer token; Honcho deployments with
//!   `AUTH_USE_AUTH=false` ignore it.
//! - `HONCHO_TIMEOUT_MS`: request timeout for write-backs (default 10000).
//! - `HONCHO_INJECT_TIMEOUT_MS`: budget for the per-turn context read
//!   (default 2500); a slow Honcho degrades to no injected memory.

use std::{env, fmt, sync::Arc, time::Duration};

use reqwest::StatusCode;
use serde_json::{Value, json};
use tracing::warn;

const COMPONENT: &str = "session_runtime";
const DEFAULT_WORKSPACE: &str = "centaur";
const DEFAULT_WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const DEFAULT_INJECT_TIMEOUT: Duration = Duration::from_millis(2500);
/// Cap on the injected context block so a large representation cannot crowd
/// out the turn's actual input.
const MAX_CONTEXT_BLOCK_CHARS: usize = 6_000;
/// Honcho resource ids must match `^[a-zA-Z0-9_-]+$` (max 512 chars); centaur
/// thread keys and Slack ids carry `:` and `.`, which map to `-`.
const MAX_ID_CHARS: usize = 512;

#[derive(Debug)]
pub(crate) struct HonchoError(String);

impl fmt::Display for HonchoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for HonchoError {}

#[derive(Clone)]
pub(crate) struct HonchoMemory {
    client: reqwest::Client,
    base_url: Arc<str>,
    workspace: Arc<str>,
    api_key: Option<Arc<str>>,
    inject_timeout: Duration,
}

/// One message to append to a Honcho session.
pub(crate) struct HonchoTurnMessage {
    pub peer_id: String,
    pub content: String,
}

impl HonchoMemory {
    pub(crate) fn from_env() -> Option<Self> {
        let enabled = env::var("HONCHO_ENABLED").ok()?;
        if !matches!(
            enabled.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ) {
            return None;
        }
        let base_url = env::var("HONCHO_BASE_URL")
            .ok()?
            .trim()
            .trim_end_matches('/')
            .to_owned();
        if base_url.is_empty() {
            warn!(
                component = COMPONENT,
                event = "honcho_disabled_missing_base_url",
                "HONCHO_ENABLED is set but HONCHO_BASE_URL is empty; Honcho memory disabled"
            );
            return None;
        }
        let workspace = env::var("HONCHO_WORKSPACE")
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
            .map(|value| sanitize_id(&value))
            .unwrap_or_else(|| DEFAULT_WORKSPACE.to_owned());
        let api_key = env::var("HONCHO_API_KEY")
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty() && value != "HONCHO_API_KEY");
        let write_timeout = env_duration("HONCHO_TIMEOUT_MS", DEFAULT_WRITE_TIMEOUT);
        let inject_timeout = env_duration("HONCHO_INJECT_TIMEOUT_MS", DEFAULT_INJECT_TIMEOUT);
        let client = reqwest::Client::builder()
            .timeout(write_timeout)
            .build()
            .ok()?;
        Some(Self {
            client,
            base_url: Arc::from(base_url),
            workspace: Arc::from(workspace),
            api_key: api_key.map(Arc::from),
            inject_timeout,
        })
    }

    pub(crate) fn inject_timeout(&self) -> Duration {
        self.inject_timeout
    }

    fn request(&self, method: reqwest::Method, path: &str) -> reqwest::RequestBuilder {
        let request = self
            .client
            .request(method, format!("{}{path}", self.base_url));
        match &self.api_key {
            Some(api_key) => request.bearer_auth(api_key.as_ref()),
            None => request,
        }
    }

    fn workspace_path(&self) -> String {
        format!("/v3/workspaces/{}", self.workspace)
    }

    /// Fetch the requester peer's context and render it as a markdown block for
    /// injection ahead of the user turn. Returns `None` when the peer is
    /// unknown to Honcho or has no derived memory yet.
    pub(crate) async fn peer_context_block(
        &self,
        peer_id: &str,
    ) -> Result<Option<String>, HonchoError> {
        let response = self
            .request(
                reqwest::Method::GET,
                &format!("{}/peers/{peer_id}/context", self.workspace_path()),
            )
            .send()
            .await
            .map_err(|error| HonchoError(format!("peer context request failed: {error}")))?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let body = checked_body(response, "peer context").await?;
        Ok(format_context_block(peer_id, &body))
    }

    /// Mirror a set of turn messages into the Honcho session keyed by the
    /// centaur thread key, creating workspace, peers, and session as needed
    /// (all get-or-create). Peers must be the superset of message authors.
    pub(crate) async fn record_turn(
        &self,
        thread_key: &str,
        peers: &[String],
        messages: &[HonchoTurnMessage],
    ) -> Result<(), HonchoError> {
        if messages.is_empty() {
            return Ok(());
        }
        let session_id = session_id_for_thread(thread_key);
        self.request(reqwest::Method::POST, "/v3/workspaces")
            .json(&json!({ "id": self.workspace.as_ref() }))
            .send()
            .await
            .map_err(|error| HonchoError(format!("workspace ensure failed: {error}")))?;
        let peers_payload: serde_json::Map<String, Value> = peers
            .iter()
            .map(|peer| (sanitize_id(peer), json!({})))
            .collect();
        self.request(
            reqwest::Method::POST,
            &format!("{}/sessions", self.workspace_path()),
        )
        .json(&json!({ "id": session_id, "peers": Value::Object(peers_payload) }))
        .send()
        .await
        .map_err(|error| HonchoError(format!("session ensure failed: {error}")))?;
        let payload: Vec<Value> = messages
            .iter()
            .map(|message| {
                json!({
                    "peer_id": sanitize_id(&message.peer_id),
                    "content": message.content,
                })
            })
            .collect();
        let response = self
            .request(
                reqwest::Method::POST,
                &format!("{}/sessions/{session_id}/messages", self.workspace_path()),
            )
            .json(&json!({ "messages": payload }))
            .send()
            .await
            .map_err(|error| HonchoError(format!("message append failed: {error}")))?;
        checked_body(response, "message append").await?;
        Ok(())
    }
}

async fn checked_body(response: reqwest::Response, action: &str) -> Result<Value, HonchoError> {
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|error| HonchoError(format!("{action} response read failed: {error}")))?;
    if !status.is_success() {
        return Err(HonchoError(format!("{action} returned {status}: {body}")));
    }
    Ok(serde_json::from_str(&body).unwrap_or(Value::Null))
}

fn env_duration(name: &str, default: Duration) -> Duration {
    env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|millis| *millis > 0)
        .map(Duration::from_millis)
        .unwrap_or(default)
}

/// Map a free-form identifier onto Honcho's id alphabet (`[a-zA-Z0-9_-]`,
/// max 512 chars). Returns `None` when nothing legal remains.
pub(crate) fn sanitize_id(raw: &str) -> String {
    let cleaned: String = raw
        .trim()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect();
    let cleaned = cleaned.trim_matches('-');
    cleaned.chars().take(MAX_ID_CHARS).collect()
}

/// Honcho session id for a centaur thread key (`slack:C…:ts` → `slack-C…-ts`).
pub(crate) fn session_id_for_thread(thread_key: &str) -> String {
    sanitize_id(thread_key)
}

/// Canonical Honcho peer id for a requester: `<platform>:<user id>` sanitized,
/// e.g. `slack:U0123ABC` → `slack-U0123ABC`. Prefers the resolved
/// `slack_user_id` over the raw message `user_id`.
pub(crate) fn requester_peer_id(metadata: &Value) -> Option<String> {
    let user_id = metadata
        .get("slack_user_id")
        .and_then(Value::as_str)
        .or_else(|| metadata.get("user_id").and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty())?;
    let platform = metadata
        .get("platform")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("user");
    Some(format!("{platform}:{user_id}"))
}

/// Canonical Honcho peer id for the agent: `agent:<persona>`.
pub(crate) fn agent_peer_id(persona_id: Option<&str>) -> String {
    format!("agent:{}", persona_id.unwrap_or("default"))
}

/// Extract the user-authored text from session message parts, dropping the
/// context blocks the platform prepends (`# Requester Context`, `# Slack …`)
/// and non-text parts. Returns `None` when no user text remains.
pub(crate) fn honcho_text_from_parts(parts: &[Value]) -> Option<String> {
    const CONTEXT_BLOCK_PREFIXES: &[&str] = &[
        "# Requester Context",
        "# Slack Session Context",
        "# Slack Thread Context",
        "# Current Request",
        "# Memory Context",
        "This Slack thread was just restarted",
    ];
    let mut texts: Vec<&str> = Vec::new();
    for part in parts {
        if part.get("type").and_then(Value::as_str) != Some("text") {
            continue;
        }
        let Some(text) = part.get("text").and_then(Value::as_str) else {
            continue;
        };
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        if CONTEXT_BLOCK_PREFIXES
            .iter()
            .any(|prefix| text.starts_with(prefix))
        {
            continue;
        }
        texts.push(text);
    }
    if texts.is_empty() {
        return None;
    }
    Some(texts.join("\n"))
}

/// Render the Honcho peer context response (`representation` + `peer_card`)
/// as the markdown block injected ahead of a user turn.
fn format_context_block(peer_id: &str, body: &Value) -> Option<String> {
    let representation = body
        .get("representation")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let peer_card: Vec<&str> = body
        .get("peer_card")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .collect()
        })
        .unwrap_or_default();
    if representation.is_none() && peer_card.is_empty() {
        return None;
    }
    let mut block = format!(
        "# Memory Context (Honcho)\n\nDurable memory about the requester (peer `{peer_id}`) \
         from prior conversations, provided automatically by the platform. It may be \
         incomplete or stale; treat it as context, not ground truth.\n"
    );
    if let Some(representation) = representation {
        block.push_str("\n## What is known about the requester\n\n");
        block.push_str(representation);
        block.push('\n');
    }
    if !peer_card.is_empty() {
        block.push_str("\n## Peer card\n\n");
        for item in peer_card {
            block.push_str("- ");
            block.push_str(item);
            block.push('\n');
        }
    }
    block.push_str("\n---");
    if block.len() > MAX_CONTEXT_BLOCK_CHARS {
        block.truncate(MAX_CONTEXT_BLOCK_CHARS);
        block.push_str("\n…(memory truncated)");
    }
    Some(block)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitize_id_maps_thread_keys() {
        assert_eq!(
            sanitize_id("slack:C0TEST12345:1791670752.873056"),
            "slack-C0TEST12345-1791670752-873056"
        );
        assert_eq!(sanitize_id("slack:U0123ABC"), "slack-U0123ABC");
        assert_eq!(sanitize_id("agent:chief"), "agent-chief");
    }

    #[test]
    fn requester_peer_id_prefers_slack_user_id() {
        let metadata = json!({
            "platform": "slack",
            "user_id": "raw-author",
            "slack_user_id": "U0123ABC",
        });
        assert_eq!(
            requester_peer_id(&metadata).as_deref(),
            Some("slack:U0123ABC")
        );
        let metadata = json!({ "platform": "discord", "user_id": "1234" });
        assert_eq!(
            requester_peer_id(&metadata).as_deref(),
            Some("discord:1234")
        );
        assert!(requester_peer_id(&json!({ "platform": "slack" })).is_none());
    }

    #[test]
    fn honcho_text_from_parts_drops_context_blocks() {
        let parts = vec![
            json!({ "type": "text", "text": "# Requester Context\n\nThe Slack user…\n---" }),
            json!({ "type": "text", "text": "my favorite editor is edlin" }),
            json!({ "type": "attachment", "name": "a.png" }),
            json!({ "type": "text", "text": "# Slack Thread Context\n\nEarlier…" }),
        ];
        assert_eq!(
            honcho_text_from_parts(&parts).as_deref(),
            Some("my favorite editor is edlin")
        );
        assert!(honcho_text_from_parts(&[]).is_none());
        assert!(
            honcho_text_from_parts(&[json!({ "type": "text", "text": "# Requester Context\n…" })])
                .is_none()
        );
    }

    #[test]
    fn format_context_block_empty_memory_is_none() {
        assert!(format_context_block("slack-U1", &json!({ "representation": "" })).is_none());
        assert!(
            format_context_block(
                "slack-U1",
                &json!({ "representation": null, "peer_card": null })
            )
            .is_none()
        );
    }

    #[test]
    fn format_context_block_renders_representation_and_card() {
        let block = format_context_block(
            "slack-U1",
            &json!({
                "representation": "Prefers edlin.",
                "peer_card": ["uses nix", "oncall"],
            }),
        )
        .expect("block");
        assert!(block.starts_with("# Memory Context (Honcho)"));
        assert!(block.contains("Prefers edlin."));
        assert!(block.contains("- uses nix"));
        assert!(block.contains("slack-U1"));
    }
}
