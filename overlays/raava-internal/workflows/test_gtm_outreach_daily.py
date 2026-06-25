"""Tests for the GTM outreach scheduled workflow."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def _load_workflow_module():
    path = Path(__file__).with_name("gtm_outreach_daily.py")
    spec = importlib.util.spec_from_file_location("gtm_outreach_daily_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load GTM outreach workflow")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workflow = _load_workflow_module()


class FakeContext:
    def __init__(self, producer_result: dict):
        self.producer_result = producer_result
        self.tool_calls: list[tuple[str, str, dict]] = []
        self.agent_turn_calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool: str, method: str, args: dict):
        self.tool_calls.append((tool, method, args))
        if (tool, method) == ("raava_outreach", "produce"):
            return self.producer_result
        if (tool, method) == ("slack", "send_message"):
            return {"ok": True}
        raise AssertionError(f"unexpected tool call {tool}.{method}")

    async def agent_turn(self, prompt: str, **kwargs):
        self.agent_turn_calls.append((prompt, kwargs))
        return {"result_text": "Top pick: Acme has a timely trigger."}


def _slack_call(ctx: FakeContext) -> dict:
    calls = [args for tool, method, args in ctx.tool_calls if (tool, method) == ("slack", "send_message")]
    assert len(calls) == 1
    return calls[0]


@pytest.mark.asyncio
async def test_handler_posts_pending_only_blocks_with_fingerprints():
    producer_result = {
        "kept_drafts": [
            {
                "rank": 1,
                "queue_id": "q-1",
                "status": "pending",
                "account": "Acme Roofing",
                "why_now": "Hiring ops leaders this week",
                "score": 93,
            },
            {
                "rank": 2,
                "queue_id": "q-2",
                "status": "delivered",
                "account": "SentCo",
                "why_now": "Already sent",
                "score": 88,
            },
            {
                "rank": 3,
                "queue_id": "q-3",
                "status": "pending",
                "lead": {"company": "Northstar"},
                "why_now": "New territory expansion",
                "score": 86,
            },
            {
                "rank": 4,
                "queue_id": "q-4",
                "queue_status": "pending",
                "company": "Brightline",
                "trigger": "Fresh procurement signal",
                "priority_score": 81,
            },
        ]
    }
    ctx = FakeContext(producer_result)

    result = await workflow.handler({}, ctx)

    assert result["pending_count"] == 3
    slack_args = _slack_call(ctx)
    assert slack_args["channel"] == workflow.SLACK_CHANNEL
    assert slack_args["text"] == "GTM outreach: 3 pending drafts."
    rendered = "\n".join(block.get("text", {}).get("text", "") for block in slack_args["blocks"])
    assert "Acme Roofing" in rendered
    assert "Northstar" in rendered
    assert "Brightline" in rendered
    assert "SentCo" not in rendered
    assert "queue_id `q-1`" in rendered
    assert "fp `" in rendered
    fingerprint = workflow.draft_fingerprint(producer_result["kept_drafts"][0])
    assert fingerprint in rendered
    assert any(block.get("block_id") == f"gtm-draft-q-1-{fingerprint}" for block in slack_args["blocks"])


@pytest.mark.asyncio
async def test_handler_empty_result_posts_no_signals_and_skips_pre_read():
    ctx = FakeContext({"kept_drafts": []})

    result = await workflow.handler({"slack_channel": "custom-gtm"}, ctx)

    assert result["pending_count"] == 0
    slack_args = _slack_call(ctx)
    assert slack_args["channel"] == "custom-gtm"
    assert slack_args["text"] == "GTM outreach: no signals today."
    assert "no signals today" in slack_args["blocks"][0]["text"]["text"]
    assert ctx.agent_turn_calls == []


@pytest.mark.asyncio
async def test_pre_read_agent_turn_is_scheduled_origin_without_outreach_send_authority():
    producer_result = {
        "kept_drafts": [
            {
                "rank": 1,
                "queue_id": "q-1",
                "status": "pending",
                "account": "Acme",
                "why_now": "Hiring",
                "score": 91,
            }
        ]
    }
    ctx = FakeContext(producer_result)

    await workflow.handler({}, ctx)

    assert len(ctx.agent_turn_calls) == 1
    prompt, kwargs = ctx.agent_turn_calls[0]
    assert "read-only brief" in prompt
    assert "outreach_send" in prompt
    assert kwargs["persona"] is None
    assert kwargs["harness"] == "codex"
    assert kwargs["delivery"] == {"platform": "dev"}
    assert "scheduled read-only" in kwargs["agents_md_override"]
    assert "outreach_send` tool is not\navailable" in kwargs["agents_md_override"]
    assert kwargs["user_id"] is None
    assert kwargs["metadata"]["scheduled_origin"] is True
    assert kwargs["metadata"]["forbidden_tools"] == ["outreach_send"]


@pytest.mark.asyncio
async def test_handler_renders_real_keptdraft_shape_without_status_field():
    # Real ProducerResult.kept_drafts items carry NO status field
    # (KeptDraft = rank/account/why_now/queue_id/score). They are pending by
    # construction and MUST still render -- guards against the status-filter
    # regression that made the landing zone always say "no signals".
    producer_result = {
        "kept_drafts": [
            {"rank": 1, "queue_id": "q-1", "account": "Acme", "why_now": "Hiring", "score": 90},
            {"rank": 2, "queue_id": "q-2", "account": "Northstar", "why_now": "Expansion", "score": 85},
        ]
    }
    ctx = FakeContext(producer_result)

    result = await workflow.handler({}, ctx)

    assert result["pending_count"] == 2
    slack_args = _slack_call(ctx)
    assert slack_args["text"] == "GTM outreach: 2 pending drafts."
    rendered = "\n".join(b.get("text", {}).get("text", "") for b in slack_args["blocks"])
    assert "Acme" in rendered
    assert "Northstar" in rendered
