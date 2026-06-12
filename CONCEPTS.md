# Concepts

Shared domain vocabulary for Centaur. Terms here mean something specific in
this codebase and should be reused by docs, issues, and agent instructions
without redefinition.

## Runtime Model

### Sandbox Runtime

An isolated execution environment that owns one agent conversation's harness
process, tool callback credentials, and network policy boundary.

### Harness

The command-line agent runtime that Centaur starts inside a sandbox runtime to
turn a persisted user message into assistant output.

### Harness Auth Mode

The credential posture a harness uses when it starts, such as mounted OAuth,
brokered token auth, or proxy-backed API-key placeholder auth.

If more than one auth signal is present, startup precedence matters: the active
harness auth mode is the one the harness actually selects, not merely the one
the deployment intended.

## Credential Boundary

### Credential Placeholder

A harmless stand-in secret value that lets a harness or SDK initialize while a
trusted proxy or broker supplies the real credential outside the sandbox.

Credential placeholders must not be mixed with a different intended harness
auth mode unless the startup rules explicitly choose the intended mode.

### Iron Proxy

The sandbox egress proxy that mediates outbound provider traffic and applies
credential injection or network controls outside the sandbox runtime.

## Extension Model

### Overlay

A deployment layer that adds or overrides Centaur behavior for an organization
without turning the upstream base platform into organization-specific code.

### Persona

A named operating identity that Centaur can apply to a sandbox runtime so the
harness behaves like a specific role or function lead.
