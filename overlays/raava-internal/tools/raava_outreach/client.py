"""Raava outreach loop surface — subprocess shell-out to the raava-outreach CLI.

The loop binary keeps its own DB/email/Slack secrets in its own environment.
This tool only shells out to it and parses the output; it never imports loop
modules in-process.

Binary resolution (highest priority wins):
  1. RAAVA_OUTREACH_BIN env var
  2. Default: /Users/master/raava-outreach/.venv/bin/raava-outreach
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

_DEFAULT_BIN = "/Users/master/raava-outreach/.venv/bin/raava-outreach"


def _bin() -> str:
    return os.getenv("RAAVA_OUTREACH_BIN", _DEFAULT_BIN)


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [_bin()] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _require_ok(result: subprocess.CompletedProcess, context: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"raava-outreach {context} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _parse_json(result: subprocess.CompletedProcess, context: str) -> dict[str, Any]:
    _require_ok(result, context)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"raava-outreach {context}: could not parse JSON output: {exc}\n"
            f"stdout={result.stdout!r}"
        ) from exc


def _parse_text(result: subprocess.CompletedProcess, context: str) -> dict[str, Any]:
    _require_ok(result, context)
    return {"raw": result.stdout.strip()}


def _parse_send_approved_output(stdout: str) -> dict[str, Any]:
    """Parse the text summary line from send_approved.

    Expected format (approximate):
        sent=2 blocked=1 skipped=0 (of 3 approved)
    """
    raw = stdout.strip()
    result: dict[str, Any] = {"raw": raw}

    # Extract key=value pairs
    for key in ("sent", "blocked", "skipped"):
        m = re.search(rf"{key}=(\d+)", raw)
        if m:
            result[key] = int(m.group(1))

    # Extract total approved
    m = re.search(r"of\s+(\d+)\s+approved", raw)
    if m:
        result["approved_total"] = int(m.group(1))

    return result


class RaavaOutreachClient:
    """Safe subprocess surface over the raava-outreach CLI.

    The tool faithfully exposes all loop commands including approve/send_approved.
    The never-send-unless-explicitly-told rule lives in the outreach-operator
    persona (PROMPT.md), not here by method omission.  The loop's own send guards
    (proof_cleared, bench, cap, suppression) remain the technical backstop.
    """

    # ------------------------------------------------------------------ #
    # Produce
    # ------------------------------------------------------------------ #

    def produce(self, dry_run: bool = True) -> dict[str, Any]:
        """Run the produce step (generate outreach drafts).

        Args:
            dry_run: When True (default) runs with --dry-run; set False for live.

        Returns:
            Parsed JSON dict from the CLI.
        """
        mode_flag = "--dry-run" if dry_run else "--live"
        result = _run(["produce", mode_flag, "--format", "json"])
        return _parse_json(result, "produce")

    # ------------------------------------------------------------------ #
    # Queue
    # ------------------------------------------------------------------ #

    def queue(
        self,
        status: str | None = None,
        track: str | None = None,
    ) -> dict[str, Any]:
        """List the outreach queue.

        Args:
            status: Filter by status (e.g. "pending", "approved", "sent").
            track: Filter by track/campaign name.

        Returns:
            Parsed JSON dict from the CLI.
        """
        args = ["queue", "--format", "json"]
        if status:
            args += ["--status", status]
        if track:
            args += ["--track", track]
        result = _run(args)
        return _parse_json(result, "queue")

    # ------------------------------------------------------------------ #
    # Triage
    # ------------------------------------------------------------------ #

    def triage(self) -> dict[str, Any]:
        """Run triage (re-score and sort the queue).

        Returns:
            Parsed JSON dict from the CLI.
        """
        result = _run(["triage", "--format", "json"])
        return _parse_json(result, "triage")

    # ------------------------------------------------------------------ #
    # Preflight
    # ------------------------------------------------------------------ #

    def preflight(self) -> dict[str, Any]:
        """Run preflight checks before sending.

        Returns:
            Parsed JSON dict from the CLI.
        """
        result = _run(["preflight", "--format", "json"])
        return _parse_json(result, "preflight")

    # ------------------------------------------------------------------ #
    # Curation audit
    # ------------------------------------------------------------------ #

    def curation_audit(self) -> dict[str, Any]:
        """Run a curation audit of the outreach queue.

        Returns:
            Parsed JSON dict from the CLI.
        """
        result = _run(["curation-audit", "--format", "json"])
        return _parse_json(result, "curation-audit")

    # ------------------------------------------------------------------ #
    # Reject
    # ------------------------------------------------------------------ #

    def reject(self, entry_id: str) -> dict[str, Any]:
        """Reject an outreach entry by ID.

        Args:
            entry_id: The queue entry ID to reject.

        Returns:
            Dict with "raw" key containing CLI text output.
        """
        result = _run(["reject", str(entry_id)])
        return _parse_text(result, f"reject {entry_id}")

    # ------------------------------------------------------------------ #
    # Approve
    # ------------------------------------------------------------------ #

    def approve(self, entry_id: str | None = None, all: bool = False) -> dict[str, Any]:
        """Approve one or all outreach entries.

        Args:
            entry_id: The specific queue entry ID to approve.
            all: If True, approve all pending entries (entry_id is ignored).

        Returns:
            Dict with "raw" key containing CLI text output.
        """
        if all:
            args = ["approve", "--all"]
        elif entry_id is not None:
            args = ["approve", str(entry_id)]
        else:
            raise ValueError("approve() requires either entry_id or all=True")
        result = _run(args)
        return _parse_text(result, "approve")

    # ------------------------------------------------------------------ #
    # Send approved
    # ------------------------------------------------------------------ #

    def send_approved(self, cap: int | None = None) -> dict[str, Any]:
        """Send all approved outreach entries (subject to loop guards).

        The loop enforces its own send guards (proof_cleared, bench, suppression,
        cap) regardless of what this method requests.  If the guard blocks a send
        the returned dict will have blocked > 0 and sent == 0 for that entry.

        Args:
            cap: Maximum number of sends in this run.

        Returns:
            Dict with "raw", "sent", "blocked", "skipped", "approved_total" keys.
        """
        args = ["send-approved"]
        if cap is not None:
            args += ["--cap", str(cap)]
        result = _run(args)
        _require_ok(result, "send-approved")
        return _parse_send_approved_output(result.stdout)


def _client() -> RaavaOutreachClient:
    """Factory for tool SDK integration."""
    return RaavaOutreachClient()
