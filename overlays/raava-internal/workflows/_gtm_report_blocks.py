"""Block Kit rendering helpers for the GTM outreach landing zone."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_MAX_ITEMS = 20


def _pick(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _lead_value(entry: dict[str, Any], *keys: str) -> Any:
    lead = entry.get("lead")
    if isinstance(lead, dict):
        value = _pick(lead, *keys)
        if value not in (None, ""):
            return value
    return _pick(entry, *keys)


def _normalize_status(entry: dict[str, Any]) -> str:
    return str(_pick(entry, "status", "queue_status", default="")).strip().lower()


def _normalize_account(entry: dict[str, Any]) -> str:
    account = _lead_value(entry, "account", "company", "company_name", "name")
    return str(account or "Unknown account").strip()


def _normalize_queue_id(entry: dict[str, Any]) -> str:
    queue_id = _pick(entry, "queue_id", "entry_id", "id")
    return str(queue_id or "").strip()


def _normalize_rank(entry: dict[str, Any], index: int) -> str:
    rank = _pick(entry, "rank", default=index + 1)
    return str(rank).strip()


def _normalize_score(entry: dict[str, Any]) -> str:
    score = _pick(entry, "score", "priority_score", default="")
    return str(score).strip()


def _normalize_why_now(entry: dict[str, Any]) -> str:
    why_now = _pick(entry, "why_now", "trigger", "reason", default="")
    return str(why_now or "No trigger provided.").strip()


def draft_fingerprint(entry: dict[str, Any]) -> str:
    """Return a short stable fingerprint for stale landing-zone card checks."""

    payload = {
        "queue_id": _normalize_queue_id(entry),
        "account": _normalize_account(entry),
        "why_now": _normalize_why_now(entry),
        "score": _normalize_score(entry),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:10]


def pending_drafts(producer_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Drafts to surface in the landing zone: pending, or status-absent.

    A producer pass emits ``kept_drafts`` summaries (KeptDraft = rank/account/
    why_now/queue_id/score) that carry NO status field -- they are freshly kept
    and pending by construction. Treat absent/empty status as pending so the real
    producer shape renders; still exclude anything explicitly non-pending
    (delivered/rejected) defensively.
    """

    raw = producer_result.get("kept_drafts")
    if raw is None:
        raw = producer_result.get("drafts", [])
    if not isinstance(raw, list):
        return []

    drafts = [
        item
        for item in raw
        if isinstance(item, dict) and _normalize_status(item) in ("", "pending")
    ]
    return drafts[:_MAX_ITEMS]


def build_gtm_report_blocks(producer_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Render pending drafts into Slack Block Kit sections."""

    drafts = pending_drafts(producer_result)
    if not drafts:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*GTM outreach*: no signals today.",
                },
            }
        ]

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "GTM outreach landing zone"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Pending drafts from the routine. Fetch a fresh draft before staging any send.",
            },
        },
    ]
    for index, draft in enumerate(drafts):
        queue_id = _normalize_queue_id(draft)
        account = _normalize_account(draft)
        rank = _normalize_rank(draft, index)
        score = _normalize_score(draft) or "n/a"
        why_now = _normalize_why_now(draft)
        fingerprint = draft_fingerprint(draft)
        blocks.append(
            {
                "type": "section",
                "block_id": f"gtm-draft-{queue_id or index}-{fingerprint}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*#{rank} {account}*  score `{score}`\n"
                        f"{why_now}\n"
                        f"queue_id `{queue_id}`  fp `{fingerprint}`"
                    ),
                },
            }
        )
    return blocks


def report_text(producer_result: dict[str, Any]) -> str:
    count = len(pending_drafts(producer_result))
    if count == 0:
        return "GTM outreach: no signals today."
    return f"GTM outreach: {count} pending draft{'s' if count != 1 else ''}."
