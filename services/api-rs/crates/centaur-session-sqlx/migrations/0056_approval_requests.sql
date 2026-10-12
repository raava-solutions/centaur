-- Durable approval requests. An agent action that needs operator approval
-- creates one row; the decision (approve/deny) arrives out of band (Slack
-- interactive button) and binds the recorded action_hash, so a different
-- action always needs a fresh request. Decisions are one-shot: the atomic
-- decide update only fires while status = 'pending'.
create table approval_requests (
    id text primary key,
    thread_key text not null,
    execution_id text,
    action_summary text not null,
    action_hash text not null,
    requester text not null default '',
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'denied', 'expired')),
    approver text,
    decided_at timestamptz,
    expires_at timestamptz not null,
    slack_channel_id text,
    slack_message_ts text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index approval_requests_thread_created
    on approval_requests (thread_key, created_at desc);

-- The Slack notifier polls undelivered pending requests; keep that scan cheap.
create index approval_requests_pending_delivery
    on approval_requests (created_at)
    where status = 'pending' and slack_message_ts is null;
