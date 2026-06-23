"""Tests for the raava_outreach CLI client.

All subprocess calls are mocked — no live CLI invocations.

Import note: conftest.py at overlays/raava-internal/conftest.py inserts both
the repo root (for centaur_sdk) and the overlay tools dir (for raava_outreach)
onto sys.path, so plain "from raava_outreach.client import ..." works.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Fallback: ensure the overlay tools dir is on path even when running this
# test file directly (conftest.py does this under pytest, but direct execution
# needs it too).
_TOOLS_DIR = str(Path(__file__).resolve().parents[1])
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from raava_outreach.client import (  # noqa: E402
    RaavaOutreachClient,
    _parse_send_approved_output,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Happy-path: queue
# ---------------------------------------------------------------------------

def test_queue_parses_json(monkeypatch):
    payload = {"entries": [{"id": "abc", "status": "pending", "company": "Acme"}]}

    def mock_run(cmd, **kwargs):
        assert "--status" in cmd
        assert "pending" in cmd
        assert "--format" in cmd
        assert "json" in cmd
        return _make_result(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.queue(status="pending")
    assert result == payload


# ---------------------------------------------------------------------------
# Happy-path: produce
# ---------------------------------------------------------------------------

def test_produce_dry_run_parses_json(monkeypatch):
    payload = {"campaign": "q3-roofing", "drafts": 5}

    def mock_run(cmd, **kwargs):
        assert "--dry-run" in cmd
        assert "--format" in cmd
        return _make_result(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.produce(dry_run=True)
    assert result["campaign"] == "q3-roofing"
    assert result["drafts"] == 5


# ---------------------------------------------------------------------------
# Happy-path: approve
# ---------------------------------------------------------------------------

def test_approve_by_id(monkeypatch):
    def mock_run(cmd, **kwargs):
        assert "approve" in cmd
        assert "entry-42" in cmd
        assert "--all" not in cmd
        return _make_result(stdout="approved entry-42")

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.approve(entry_id="entry-42")
    assert result["raw"] == "approved entry-42"


def test_approve_all(monkeypatch):
    def mock_run(cmd, **kwargs):
        assert "--all" in cmd
        return _make_result(stdout="approved 3 entries")

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.approve(all=True)
    assert "3" in result["raw"]


# ---------------------------------------------------------------------------
# Happy-path: send_approved
# ---------------------------------------------------------------------------

def test_send_approved_parses_summary(monkeypatch):
    summary = "sent=2 blocked=0 skipped=1 (of 3 approved)"

    def mock_run(cmd, **kwargs):
        assert "send-approved" in cmd
        return _make_result(stdout=summary)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.send_approved()
    assert result["sent"] == 2
    assert result["blocked"] == 0
    assert result["skipped"] == 1
    assert result["approved_total"] == 3


def test_send_approved_with_cap(monkeypatch):
    summary = "sent=1 blocked=0 skipped=0 (of 1 approved)"

    def mock_run(cmd, **kwargs):
        assert "--cap" in cmd
        assert "5" in cmd
        return _make_result(stdout=summary)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.send_approved(cap=5)
    assert result["sent"] == 1


# ---------------------------------------------------------------------------
# Mandatory guard-binding test:
# When the CLI reports blocked > 0 and sent == 0, the tool surfaces that
# faithfully — it cannot override the loop's send guards.
# ---------------------------------------------------------------------------

def test_send_approved_guard_binding_blocked(monkeypatch):
    """The tool must surface 'blocked' when the loop guard fires.

    The loop's guards (proof_cleared, bench, cap, suppression) can block sends.
    This test asserts the tool surfaces blocked=1, sent=0 — it does NOT silently
    report a send that didn't happen and cannot override the guard.
    """
    guard_output = "sent=0 blocked=1 skipped=0 (of 1 approved)"

    def mock_run(cmd, **kwargs):
        return _make_result(stdout=guard_output)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    result = client.send_approved()

    assert result["sent"] == 0, "Tool must NOT report a send when the guard blocked it"
    assert result["blocked"] == 1, "Tool must surface the blocked count from the loop guard"


# ---------------------------------------------------------------------------
# Edge: non-zero exit surfaces as RuntimeError, not a crash
# ---------------------------------------------------------------------------

def test_queue_nonzero_exit_raises(monkeypatch):
    def mock_run(cmd, **kwargs):
        return _make_result(stdout="", stderr="DB connection failed", returncode=1)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    with pytest.raises(RuntimeError, match="failed"):
        client.queue()


def test_produce_nonzero_exit_raises(monkeypatch):
    def mock_run(cmd, **kwargs):
        return _make_result(stdout="", stderr="error: missing campaign", returncode=2)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    with pytest.raises(RuntimeError):
        client.produce()


# ---------------------------------------------------------------------------
# Edge: unparseable JSON raises RuntimeError
# ---------------------------------------------------------------------------

def test_queue_bad_json_raises(monkeypatch):
    def mock_run(cmd, **kwargs):
        return _make_result(stdout="this is not json", returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RaavaOutreachClient()
    with pytest.raises(RuntimeError, match="JSON"):
        client.queue()


# ---------------------------------------------------------------------------
# Unit: _parse_send_approved_output edge cases
# ---------------------------------------------------------------------------

def test_parse_send_approved_partial_line():
    result = _parse_send_approved_output("sent=0 blocked=2 skipped=0 (of 2 approved)")
    assert result["sent"] == 0
    assert result["blocked"] == 2
    assert result["approved_total"] == 2
