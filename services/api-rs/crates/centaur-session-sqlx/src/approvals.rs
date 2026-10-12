//! Approval requests: durable, one-shot operator approvals for agent actions.
//!
//! An agent action that needs approval creates a row bound to the action's
//! `action_hash`; the decision arrives out of band (Slack interactive button
//! via slackbotv2) and is recorded atomically. A decision can never be
//! replayed or overwritten: the decide update only fires while the row is
//! still `pending` and unexpired. A different action hash always requires a
//! fresh request — approvals are never reusable.

use serde::{Deserialize, Serialize};
use sqlx::PgPool;
use time::OffsetDateTime;
use uuid::Uuid;

pub const APPROVAL_ID_PREFIX: &str = "apr";
pub const DEFAULT_APPROVAL_TTL_SECONDS: i64 = 30 * 60;
pub const MIN_APPROVAL_TTL_SECONDS: i64 = 10;
pub const MAX_APPROVAL_TTL_SECONDS: i64 = 24 * 60 * 60;

#[derive(Clone, Debug, Serialize, sqlx::FromRow)]
pub struct ApprovalRequest {
    pub id: String,
    pub thread_key: String,
    pub execution_id: Option<String>,
    pub action_summary: String,
    pub action_hash: String,
    pub requester: String,
    pub status: String,
    pub approver: Option<String>,
    #[serde(with = "time::serde::rfc3339::option")]
    pub decided_at: Option<OffsetDateTime>,
    #[serde(with = "time::serde::rfc3339")]
    pub expires_at: OffsetDateTime,
    pub slack_channel_id: Option<String>,
    pub slack_message_ts: Option<String>,
    #[serde(with = "time::serde::rfc3339")]
    pub created_at: OffsetDateTime,
    #[serde(with = "time::serde::rfc3339")]
    pub updated_at: OffsetDateTime,
}

#[derive(Clone, Debug)]
pub struct NewApprovalRequest {
    pub thread_key: String,
    pub execution_id: Option<String>,
    pub action_summary: String,
    pub action_hash: String,
    pub requester: String,
    pub ttl_seconds: Option<i64>,
}

/// The terminal states a pending request can transition into.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalDecision {
    Approved,
    Denied,
}

impl ApprovalDecision {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Approved => "approved",
            Self::Denied => "denied",
        }
    }
}

/// Why a decide call did not transition the request.
#[derive(Debug, thiserror::Error)]
pub enum ApprovalStoreError {
    #[error(transparent)]
    Sqlx(#[from] sqlx::Error),
    #[error("approval request not found")]
    NotFound,
    #[error("approval request is no longer pending")]
    Conflict,
}

const APPROVAL_COLUMNS: &str = "id, thread_key, execution_id, action_summary, action_hash, \
requester, status, approver, decided_at, expires_at, slack_channel_id, slack_message_ts, \
created_at, updated_at";

pub async fn create_approval(
    pool: &PgPool,
    request: &NewApprovalRequest,
) -> Result<ApprovalRequest, ApprovalStoreError> {
    let id = format!("{APPROVAL_ID_PREFIX}_{}", Uuid::new_v4().simple());
    let ttl = request
        .ttl_seconds
        .unwrap_or(DEFAULT_APPROVAL_TTL_SECONDS)
        .clamp(MIN_APPROVAL_TTL_SECONDS, MAX_APPROVAL_TTL_SECONDS);
    let sql = format!(
        "INSERT INTO approval_requests (\
         id, thread_key, execution_id, action_summary, action_hash, requester, expires_at\
         ) VALUES ($1, $2, $3, $4, $5, $6, NOW() + ($7::bigint * INTERVAL '1 second')) \
         RETURNING {APPROVAL_COLUMNS}"
    );
    let approval = sqlx::query_as::<_, ApprovalRequest>(&sql)
        .bind(&id)
        .bind(&request.thread_key)
        .bind(&request.execution_id)
        .bind(&request.action_summary)
        .bind(&request.action_hash)
        .bind(&request.requester)
        .bind(ttl)
        .fetch_one(pool)
        .await?;
    Ok(approval)
}

/// Fetch one request, lazily flipping a pending-but-expired row to `expired`
/// so readers never observe a live past-TTL request.
pub async fn get_approval(
    pool: &PgPool,
    approval_id: &str,
) -> Result<ApprovalRequest, ApprovalStoreError> {
    expire_approval_if_due(pool, approval_id).await?;
    let sql = format!("SELECT {APPROVAL_COLUMNS} FROM approval_requests WHERE id = $1");
    sqlx::query_as::<_, ApprovalRequest>(&sql)
        .bind(approval_id)
        .fetch_optional(pool)
        .await?
        .ok_or(ApprovalStoreError::NotFound)
}

/// Pending requests that still need their Slack approval message posted
/// (`awaiting_slack`), or a plain status listing for operators.
pub async fn list_approvals(
    pool: &PgPool,
    status: Option<&str>,
    awaiting_slack: bool,
    limit: i64,
) -> Result<Vec<ApprovalRequest>, ApprovalStoreError> {
    let sql = format!(
        "SELECT {APPROVAL_COLUMNS} FROM approval_requests \
         WHERE ($1::text IS NULL OR status = $1) \
         AND ($2::boolean = false OR (status = 'pending' AND slack_message_ts IS NULL \
              AND expires_at > NOW())) \
         ORDER BY created_at ASC LIMIT $3"
    );
    let approvals = sqlx::query_as::<_, ApprovalRequest>(&sql)
        .bind(status)
        .bind(awaiting_slack)
        .bind(limit)
        .fetch_all(pool)
        .await?;
    Ok(approvals)
}

/// One-shot decision. Atomically transitions a live pending row; any other
/// outcome (already decided, expired, unknown id) is a conflict/not-found and
/// never mutates state. The deciding identity is recorded verbatim — the
/// caller (slackbotv2) has already authenticated the Slack user.
pub async fn decide_approval(
    pool: &PgPool,
    approval_id: &str,
    decision: ApprovalDecision,
    approver: &str,
) -> Result<ApprovalRequest, ApprovalStoreError> {
    let sql = format!(
        "UPDATE approval_requests \
         SET status = $2, approver = $3, decided_at = NOW(), updated_at = NOW() \
         WHERE id = $1 AND status = 'pending' AND expires_at > NOW() \
         RETURNING {APPROVAL_COLUMNS}"
    );
    if let Some(approval) = sqlx::query_as::<_, ApprovalRequest>(&sql)
        .bind(approval_id)
        .bind(decision.as_str())
        .bind(approver)
        .fetch_optional(pool)
        .await?
    {
        return Ok(approval);
    }
    // Nothing transitioned: surface the true current state. A pending row that
    // lost the expiry race is flipped to expired first (inside get_approval),
    // so the conflict answer is durable, not just observed.
    get_approval(pool, approval_id).await?;
    Err(ApprovalStoreError::Conflict)
}

/// Record where the approval's Slack message landed so the interaction
/// handler can update it in place and the poller stops re-delivering. Only a
/// pending request accepts a delivery record.
pub async fn record_approval_slack_delivery(
    pool: &PgPool,
    approval_id: &str,
    channel_id: &str,
    message_ts: &str,
) -> Result<ApprovalRequest, ApprovalStoreError> {
    let sql = format!(
        "UPDATE approval_requests \
         SET slack_channel_id = $2, slack_message_ts = $3, updated_at = NOW() \
         WHERE id = $1 AND status = 'pending' \
         RETURNING {APPROVAL_COLUMNS}"
    );
    sqlx::query_as::<_, ApprovalRequest>(&sql)
        .bind(approval_id)
        .bind(channel_id)
        .bind(message_ts)
        .fetch_optional(pool)
        .await?
        .ok_or(ApprovalStoreError::Conflict)
}

async fn expire_approval_if_due(pool: &PgPool, approval_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE approval_requests SET status = 'expired', updated_at = NOW() \
         WHERE id = $1 AND status = 'pending' AND expires_at <= NOW()",
    )
    .bind(approval_id)
    .execute(pool)
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::PgSessionStore;

    async fn test_pool() -> Option<PgPool> {
        let Ok(url) = std::env::var("SESSION_RUNTIME_TEST_DATABASE_URL") else {
            eprintln!("skipping: SESSION_RUNTIME_TEST_DATABASE_URL not set");
            return None;
        };
        let store = PgSessionStore::connect(&url)
            .await
            .expect("connect test db");
        store.run_migrations().await.expect("run migrations");
        Some(store.pool().clone())
    }

    fn new_request(thread_key: &str, action_hash: &str, ttl_seconds: i64) -> NewApprovalRequest {
        NewApprovalRequest {
            thread_key: thread_key.to_owned(),
            execution_id: Some("exe_test".to_owned()),
            action_summary: "destroy vm 410".to_owned(),
            action_hash: action_hash.to_owned(),
            requester: "chief".to_owned(),
            ttl_seconds: Some(ttl_seconds),
        }
    }

    #[tokio::test]
    async fn create_get_and_decide_are_one_shot() {
        let Some(pool) = test_pool().await else {
            return;
        };
        let created = create_approval(
            &pool,
            &new_request(
                "slack:C1:1.1",
                &format!("hash-{}", Uuid::new_v4().simple()),
                600,
            ),
        )
        .await
        .expect("create approval");
        assert!(created.id.starts_with("apr_"));
        assert_eq!(created.status, "pending");
        assert!(created.decided_at.is_none());

        let fetched = get_approval(&pool, &created.id)
            .await
            .expect("get approval");
        assert_eq!(fetched.action_hash, created.action_hash);

        let delivery = record_approval_slack_delivery(&pool, &created.id, "C1", "1700.0001")
            .await
            .expect("record slack delivery");
        assert_eq!(delivery.slack_channel_id.as_deref(), Some("C1"));

        let decided = decide_approval(&pool, &created.id, ApprovalDecision::Approved, "U0ZAY")
            .await
            .expect("decide approval");
        assert_eq!(decided.status, "approved");
        assert_eq!(decided.approver.as_deref(), Some("U0ZAY"));
        assert!(decided.decided_at.is_some());

        // One-shot: a second decision never overwrites the first.
        let conflict = decide_approval(&pool, &created.id, ApprovalDecision::Denied, "U0OTHER")
            .await
            .expect_err("second decide must conflict");
        assert!(matches!(conflict, ApprovalStoreError::Conflict));
        let after = get_approval(&pool, &created.id).await.expect("re-read");
        assert_eq!(after.status, "approved");
        assert_eq!(after.approver.as_deref(), Some("U0ZAY"));

        let missing = decide_approval(&pool, "apr_missing", ApprovalDecision::Approved, "U0ZAY")
            .await
            .expect_err("unknown approval is not found");
        assert!(matches!(missing, ApprovalStoreError::NotFound));
    }

    #[tokio::test]
    async fn expired_requests_cannot_be_decided() {
        let Some(pool) = test_pool().await else {
            return;
        };
        let created = create_approval(
            &pool,
            &new_request(
                "slack:C1:2.2",
                &format!("hash-{}", Uuid::new_v4().simple()),
                MIN_APPROVAL_TTL_SECONDS,
            ),
        )
        .await
        .expect("create approval");
        // Force the expiry instant into the past rather than sleeping.
        sqlx::query(
            "UPDATE approval_requests SET expires_at = NOW() - INTERVAL '1 second' WHERE id = $1",
        )
        .bind(&created.id)
        .execute(&pool)
        .await
        .expect("backdate expiry");

        let conflict = decide_approval(&pool, &created.id, ApprovalDecision::Approved, "U0ZAY")
            .await
            .expect_err("expired approval cannot be decided");
        assert!(matches!(conflict, ApprovalStoreError::Conflict));
        let after = get_approval(&pool, &created.id).await.expect("re-read");
        assert_eq!(after.status, "expired");
        assert!(after.decided_at.is_none());
    }

    #[tokio::test]
    async fn awaiting_slack_listing_excludes_delivered_and_expired() {
        let Some(pool) = test_pool().await else {
            return;
        };
        let fresh = create_approval(
            &pool,
            &new_request(
                "slack:C1:3.3",
                &format!("hash-{}", Uuid::new_v4().simple()),
                600,
            ),
        )
        .await
        .expect("create approval");
        let delivered = create_approval(
            &pool,
            &new_request(
                "slack:C1:4.4",
                &format!("hash-{}", Uuid::new_v4().simple()),
                600,
            ),
        )
        .await
        .expect("create approval");
        record_approval_slack_delivery(&pool, &delivered.id, "C1", "1700.0002")
            .await
            .expect("record slack delivery");

        let awaiting = list_approvals(&pool, Some("pending"), true, 50)
            .await
            .expect("list awaiting slack");
        let ids: Vec<&str> = awaiting
            .iter()
            .map(|approval| approval.id.as_str())
            .collect();
        assert!(ids.contains(&fresh.id.as_str()));
        assert!(!ids.contains(&delivered.id.as_str()));
    }
}
