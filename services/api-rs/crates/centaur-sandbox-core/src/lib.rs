//! Backend-neutral sandbox runtime types.
//!
//! This crate intentionally models only isolated runtime workloads. Centaur
//! concepts such as thread keys, personas, harnesses, model choice, assignment
//! generations, and durable execution rows belong in higher-level crates.

mod backend;
mod error;
mod io;
mod lifecycle;
mod spec;

/// Home directory used by the sandbox agent image.
pub const SANDBOX_AGENT_HOME: &str = "/home/agent";

/// Directory inside a sandbox where privileged access files are installed as
/// one unit. Backends require every privileged file to live under it, so
/// clearing privileged access is a single directory removal and a payload can
/// never be left half-installed.
pub const SANDBOX_PRIVILEGED_ACCESS_DIR: &str = "/home/agent/.ssh/centaur-privileged";

/// Managed ssh client config inside a sandbox. The privileged access payload
/// may install it so plain `ssh <host>` picks up the gated configuration; it is
/// cleared together with [`SANDBOX_PRIVILEGED_ACCESS_DIR`] when privileged
/// access is revoked.
pub const SANDBOX_SSH_CONFIG_PATH: &str = "/home/agent/.ssh/config";

pub use backend::{PrivilegedSandboxAccess, PrivilegedSandboxFile, SandboxBackend};
pub use error::{BoxedError, SandboxError, SandboxResult};
pub use io::{SandboxIo, SandboxIoGuard, SandboxIoParts, SandboxRead, SandboxWrite};
pub use lifecycle::{
    DesiredSandboxState, ObservedSandbox, SandboxHandle, SandboxId, SandboxStatus,
};
pub use spec::{
    EnvVar, Mount, MountKind, RepoCacheAccess, ResourceClaim, ResourceRequirements,
    SandboxCapabilities, SandboxFile, SandboxSpec,
};
