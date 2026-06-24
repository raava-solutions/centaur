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

from raava_outreach.client import RaavaOutreachClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Safety: approve() and send_approved() must NOT exist on the client
# ---------------------------------------------------------------------------

def test_approve_method_absent():
    """approve() must not be exposed — only the operator's outreach_send can send."""
    client = RaavaOutreachClient()
    assert not hasattr(client, "approve"), (
        "RaavaOutreachClient must NOT expose approve(); "
        "sending authority belongs to outreach_send only"
    )


def test_send_approved_method_absent():
    """send_approved() must not be exposed — only the operator's outreach_send can send."""
    client = RaavaOutreachClient()
    assert not hasattr(client, "send_approved"), (
        "RaavaOutreachClient must NOT expose send_approved(); "
        "sending authority belongs to outreach_send only"
    )


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
