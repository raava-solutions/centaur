-- Immutable requester snapshot for session-scoped security gates.
--
-- sessions.metadata is merged on every create-or-reuse call, so the latest
-- caller can rewrite requester fields. Gates that must bind to the session's
-- original requester (approver-gated sandbox credentials) read this column
-- instead: it is written only by the INSERT and never by conflict updates.
-- Sessions that predate this column keep NULL and therefore never match a
-- gate, which fails closed.

alter table sessions
    add column if not exists original_requester jsonb;
