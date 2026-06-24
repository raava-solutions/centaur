"""Raava outreach loop surface — subprocess shell-out to the raava-outreach CLI.

Discovery and curation ONLY. This tool cannot send email; sending is the
exclusive province of the outreach_send tool, used only by the GTM operator
on an explicit human "go."

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


class RaavaOutreachClient:
    """Safe subprocess surface over the raava-outreach CLI — discovery only.

    Exposes produce/queue/triage/preflight/curation_audit/reject.
    approve() and send_approved() are intentionally absent: this tool cannot
    send email. Sending is performed exclusively by the outreach_send tool
    at the GTM operator's explicit request.
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


def _client() -> RaavaOutreachClient:
    """Factory for tool SDK integration."""
    return RaavaOutreachClient()
