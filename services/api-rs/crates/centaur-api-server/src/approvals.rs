//! Approval request routes: agents (sandbox approval tool) create and poll
//! requests; the slackbot delivers them to Slack and records the operator's
//! signed-button decision. State lives in the `approval_requests` table
//! (migration 0056); decisions are one-shot and bound to the recorded
//! action_hash.

use axum::{
    Json,
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
};
use centaur_session_core::ThreadKey;
use centaur_session_sqlx::approvals::{
    self, ApprovalDecision, ApprovalRequest, ApprovalStoreError, NewApprovalRequest,
};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{ApiError, routes::AppState};

const MAX_ACTION_SUMMARY_CHARS: usize = 2000;
const MAX_ACTION_HASH_CHARS: usize = 128;
const MAX_REQUESTER_CHARS: usize = 256;
const MAX_LIST_LIMIT: i64 = 200;
const DEFAULT_LIST_LIMIT: i64 = 50;

#[derive(Debug, Deserialize)]
pub struct CreateApprovalRequest {
    thread_key: String,
    action_summary: String,
    action_hash: String,
    #[serde(default)]
    execution_id: Option<String>,
    #[serde(default)]
    requester: Option<String>,
    #[serde(default)]
    ttl_seconds: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct DecideApprovalRequest {
    decision: ApprovalDecision,
    approver: String,
}

#[derive(Debug, Deserialize)]
pub struct ApprovalSlackDeliveryRequest {
    channel_id: String,
    message_ts: String,
}

#[derive(Debug, Deserialize)]
pub struct ListApprovalsQuery {
    #[serde(default)]
    status: Option<String>,
    /// Only pending requests that still need their Slack message posted.
    #[serde(default)]
    awaiting_slack: bool,
    #[serde(default)]
    limit: Option<i64>,
}

pub async fn create_approval(
    State(state): State<AppState>,
    Json(request): Json<CreateApprovalRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let thread_key = ThreadKey::try_from(request.thread_key)?;
    let action_summary = bounded_trimmed(
        "action_summary",
        &request.action_summary,
        MAX_ACTION_SUMMARY_CHARS,
    )?;
    let action_hash = bounded_trimmed("action_hash", &request.action_hash, MAX_ACTION_HASH_CHARS)?;
    let requester = match request.requester.as_deref() {
        Some(value) => bounded_trimmed("requester", value, MAX_REQUESTER_CHARS)?,
        None => String::new(),
    };
    let pool = pool(&state)?;
    let approval = approvals::create_approval(
        &pool,
        &NewApprovalRequest {
            thread_key: thread_key.as_str().to_owned(),
            execution_id: request
                .execution_id
                .map(|value| value.trim().to_owned())
                .filter(|value| !value.is_empty()),
            action_summary,
            action_hash,
            requester,
            ttl_seconds: request.ttl_seconds,
        },
    )
    .await
    .map_err(approval_store_error)?;
    Ok((
        StatusCode::CREATED,
        Json(json!({ "ok": true, "approval": approval_json(&approval) })),
    ))
}

pub async fn get_approval(
    State(state): State<AppState>,
    Path(approval_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let pool = pool(&state)?;
    let approval = approvals::get_approval(&pool, &approval_id)
        .await
        .map_err(approval_store_error)?;
    Ok(Json(
        json!({ "ok": true, "approval": approval_json(&approval) }),
    ))
}

pub async fn list_approvals(
    State(state): State<AppState>,
    Query(query): Query<ListApprovalsQuery>,
) -> Result<Json<Value>, ApiError> {
    if let Some(status) = query.status.as_deref()
        && !matches!(status, "pending" | "approved" | "denied" | "expired")
    {
        return Err(ApiError::BadRequest(
            "status must be pending, approved, denied, or expired".to_owned(),
        ));
    }
    let pool = pool(&state)?;
    let approvals = approvals::list_approvals(
        &pool,
        query.status.as_deref(),
        query.awaiting_slack,
        query
            .limit
            .unwrap_or(DEFAULT_LIST_LIMIT)
            .clamp(1, MAX_LIST_LIMIT),
    )
    .await
    .map_err(approval_store_error)?;
    let approvals: Vec<Value> = approvals.iter().map(approval_json).collect();
    Ok(Json(json!({ "ok": true, "approvals": approvals })))
}

pub async fn decide_approval(
    State(state): State<AppState>,
    Path(approval_id): Path<String>,
    Json(request): Json<DecideApprovalRequest>,
) -> Result<Json<Value>, ApiError> {
    let approver = bounded_trimmed("approver", &request.approver, MAX_REQUESTER_CHARS)?;
    let pool = pool(&state)?;
    let approval = approvals::decide_approval(&pool, &approval_id, request.decision, &approver)
        .await
        .map_err(approval_store_error)?;
    Ok(Json(
        json!({ "ok": true, "approval": approval_json(&approval) }),
    ))
}

pub async fn record_approval_slack_delivery(
    State(state): State<AppState>,
    Path(approval_id): Path<String>,
    Json(request): Json<ApprovalSlackDeliveryRequest>,
) -> Result<Json<Value>, ApiError> {
    let channel_id = bounded_trimmed("channel_id", &request.channel_id, 64)?;
    let message_ts = bounded_trimmed("message_ts", &request.message_ts, 64)?;
    let pool = pool(&state)?;
    let approval =
        approvals::record_approval_slack_delivery(&pool, &approval_id, &channel_id, &message_ts)
            .await
            .map_err(|error| match error {
                // A delivery record for a decided/expired request is a conflict, but
                // so is a stale double-post of the same message; either way the row
                // keeps its first recorded destination.
                ApprovalStoreError::Conflict => ApiError::Conflict(
                    "approval request is no longer pending or unknown".to_owned(),
                ),
                other => approval_store_error(other),
            })?;
    Ok(Json(
        json!({ "ok": true, "approval": approval_json(&approval) }),
    ))
}

fn approval_json(approval: &ApprovalRequest) -> Value {
    serde_json::to_value(approval).unwrap_or_else(|_| json!({ "id": approval.id }))
}

fn bounded_trimmed(field: &'static str, value: &str, max_chars: usize) -> Result<String, ApiError> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(ApiError::BadRequest(format!("{field} must not be empty")));
    }
    if trimmed.chars().count() > max_chars {
        return Err(ApiError::BadRequest(format!(
            "{field} must be at most {max_chars} characters"
        )));
    }
    Ok(trimmed.to_owned())
}

fn approval_store_error(error: ApprovalStoreError) -> ApiError {
    match error {
        ApprovalStoreError::NotFound => ApiError::NotFound("approval request not found".to_owned()),
        ApprovalStoreError::Conflict => {
            ApiError::Conflict("approval request is already decided or expired".to_owned())
        }
        ApprovalStoreError::Sqlx(error) => ApiError::Sqlx(error),
    }
}

fn pool(state: &AppState) -> Result<sqlx::PgPool, ApiError> {
    // AppState::approvals_pool clones the pool handle (cheap Arc clone); the
    // handler keeps it alive for the duration of the call.
    state.approvals_pool()
}
