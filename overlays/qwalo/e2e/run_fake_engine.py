#!/usr/bin/env python3
"""E2E: the Qwalo overlay's engine tool, driven as an agent drives it, against a fake engine.

No deploy, no sandbox, no Slack, no credentials. The tool is invoked through the
same entrypoint the sandbox CLI shim uses (``uvx --from tools/engine engine``), so
what is verified here is the command an agent actually types.

The fake engine speaks the real contract read out of
``raava-solutions/qwaelo-ai-engine``: bearer-token service auth and the 401 / 403 /
404 / 503 error bodies of ``src/v1/auth.ts`` and ``scopeOf()`` in
``src/v1/routes.ts``, plus the read routes of the t-0027 contract. When t-0027 lands
with ``docs/v1-api.md``, re-read this file against it.

Every lead fixture is synthetic. Run it:

    python3 overlays/qwalo/e2e/run_fake_engine.py

Artifact: overlays/qwalo/e2e/reports/fake-engine.json
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

E2E_DIR = Path(__file__).resolve().parent
OVERLAY_DIR = E2E_DIR.parent
REPO_DIR = E2E_DIR.parents[2]
TOOL_DIR = OVERLAY_DIR / "tools" / "engine"
DEPLOY_DIR = OVERLAY_DIR / "deploy"
REPORT = E2E_DIR / "reports" / "fake-engine.json"

GOOD_TOKEN = "e2e-read-token"
PLACEHOLDER = "QWALO_ENGINE_TOKEN"  # the value iron-proxy's placeholder_env seeds in the sandbox
SCOPES = ["ere"]

# ---- fixtures: synthetic, no real contact data (FAILURE_MODES F7) ---------------
# Shapes are copied from the merged read surface: leadView() in
# src/v1/routes.ts, the verdict body in the same file, leadReadings() and
# scopeMetrics() in src/v1/read-db.ts. A fixture that invents a shape proves
# nothing about the engine, and this suite spent a round doing exactly that.

THRESHOLDS = {"clef": 0.85, "glm": 0.8}

# The rows, in the field set leadView() puts on the wire. `lead_id` stays off the
# wire but is what a keyset cursor is built from, so the fake keeps it internally.
LEADS = {
    "sc-synthetic-0001": {
        "lead_id": "v1:ere:sc-synthetic-0001",
        "external_id": "sc-synthetic-0001",
        "account_scope": "ere",
        "source": "v1",
        "status": "final",
        "verdict": "qualified",
        "stage": "clef",
        "probability_qualified": 0.93,
        "confidence": 0.93,
        "score": 82,
        "tier": "hot",
        "transcript_hash": "a" * 64,
        "message_count": 4,
        "label": None,
        "label_created_at": None,
        "last_error": None,
        "last_message_at": "2026-10-20T00:00:00Z",
        "updated_at": "2026-10-20T00:00:10Z",
    },
    "sc-synthetic-0002": {
        "lead_id": "v1:ere:sc-synthetic-0002",
        "external_id": "sc-synthetic-0002",
        "account_scope": "ere",
        "source": "v1",
        "status": "needs_human",
        "verdict": "cannot_tell",
        "stage": None,
        "probability_qualified": 0.51,
        "confidence": 0.4,
        "score": 41,
        "tier": "review",
        "transcript_hash": "b" * 64,
        "message_count": 2,
        "label": None,
        "label_created_at": None,
        "last_error": None,
        "last_message_at": "2026-10-20T00:05:00Z",
        "updated_at": "2026-10-20T00:05:10Z",
    },
}

# leadView(): the wire projection, and `label` nested. Nothing here carries
# seller text, a name, or a phone number (F-V15).
WIRE_FIELDS = (
    "external_id",
    "account_scope",
    "source",
    "status",
    "verdict",
    "stage",
    "probability_qualified",
    "confidence",
    "score",
    "tier",
    "label",
    "transcript_hash",
    "message_count",
    "last_error",
    "last_message_at",
    "updated_at",
)


def lead_view(row: dict) -> dict:
    out = {key: row[key] for key in WIRE_FIELDS}
    out["label"] = (
        None
        if row["label"] is None
        else {"label": row["label"], "created_at": row["label_created_at"]}
    )
    return out


# leadReadings(): the verdict rows, newest first, projected to no seller text.
READINGS = {
    "sc-synthetic-0001": [
        {
            "stage": "clef",
            "model_id": "clef-1",
            "verdict": "qualified",
            "probability": 0.93,
            "confidence": 0.93,
            "threshold": 0.85,
            "decisive": 1,
            "error": None,
            "icp_version": 3,
            "guide_version": 2,
            "examples_used": 6,
            "elapsed_ms": 1400,
            "created_at": "2026-10-20T00:00:09Z",
        }
    ],
    "sc-synthetic-0002": [
        {
            "stage": "glm",
            "model_id": "glm-1",
            "verdict": None,
            "probability": 0.51,
            "confidence": 0.4,
            "threshold": 0.8,
            "decisive": 0,
            "error": None,
            "icp_version": 3,
            "guide_version": 2,
            "examples_used": 6,
            "elapsed_ms": 5200,
            "created_at": "2026-10-20T00:05:09Z",
        }
    ],
}

# The list rows, newest first, as pageLeads() orders them.
LEAD_ROWS = [lead_view(LEADS["sc-synthetic-0002"]), lead_view(LEADS["sc-synthetic-0001"])]

# What the engine holds behind the projection: the stored transcript, with one
# message carrying a unique marker. Nothing on the read surface may repeat it; the
# answer points at the console instead (FAILURE_MODES F13, F-V15). The marker is
# ordinary-looking prose on purpose: a leak should look like a normal field, not a bug.
SELLER_MARKER = "ZEPPELIN-KITTY-4471"
SNAPSHOTS = {
    "sc-synthetic-0001": [
        {"direction": "incoming", "at": "2026-10-19T21:02:00Z", "text": "hi"},
        {
            "direction": "incoming",
            "at": "2026-10-20T00:00:00Z",
            "text": (
                "we are ready to move if the numbers work, I told my wife "
                f"{SELLER_MARKER} this week and put the deposit aside"
            ),
        },
    ],
    "sc-synthetic-0002": [
        {
            "direction": "incoming",
            "at": "2026-10-20T00:05:00Z",
            "text": f"not sure yet, sending to {SELLER_MARKER} for a second opinion",
        }
    ],
}

# The count the surface reports is the count of stored messages, so the transcript and
# the number cannot disagree.
for _external_id, _rows in SNAPSHOTS.items():
    LEADS[_external_id]["message_count"] = len(_rows)
for _row in LEAD_ROWS:
    _row["message_count"] = len(SNAPSHOTS[_row["external_id"]])

REQUEST_LOG: list = []


# ---- the fake engine: the merged read surface, nothing else ----------------------
# Routes, query rules, paging, capability checks, and error bodies follow
# src/v1/read.ts, src/v1/routes.ts, and src/v1/auth.ts on the engine's main
# branch. Implemented: GET /v1/leads, /v1/review-queue, /v1/leads/{id},
# /v1/leads/{id}/verdict, /v1/metrics, /v1/settings. Not implemented:
# /v1/documents/{kind} — no tool verb reads it yet, so the 404 this fake answers is
# its own absence and not the engine's. Recorded in the PR's "Not in this PR" list:
# this repo has no beads database, so there is no issue id to point at.

MAX_PAGE = 100
DEFAULT_PAGE = 50
WINDOWS = {"7d": 7, "30d": 30, "90d": 90, "all": None}
STATUSES = ("pending", "final", "needs_human", "error")
VERDICT_FILTERS = ("qualified", "not_qualified", "cannot_tell")
TIERS = ("hot", "warm", "cold", "review")
READ_ONLY_DOOR = frozenset(("/leads", "/review-queue", "/metrics", "/settings"))

# `caps` per slot, as SERVICE_TOKENS carries them (src/v1/auth.ts). GOOD_TOKEN is
# what a Qwalo release should hold: read only. WRITE_ONLY_TOKEN exists so a test
# can hear what the engine says when a token may not read (F-V17).
WRITE_ONLY_TOKEN = "e2e-write-only-token"
TOKEN_CAPS = {GOOD_TOKEN: ("read",), WRITE_ONLY_TOKEN: ("write",)}


def _encode_cursor(at: str, lead_id: str) -> str:
    """The engine's keyset position: base64url of [updated_at, lead_id]."""
    raw = base64.urlsafe_b64encode(json.dumps([at, lead_id]).encode()).decode()
    return raw.rstrip("=")


def _decode_cursor(raw: str) -> tuple[str, str]:
    """A position from this list, or raise the way the surface answers 400."""
    padded = raw + "=" * (-len(raw) % 4)
    parsed = json.loads(base64.urlsafe_b64decode(padded).decode())
    if (
        not isinstance(parsed, list)
        or len(parsed) != 2
        or not all(isinstance(x, str) for x in parsed)
        or not _parseable_day(parsed[0])
    ):
        raise ValueError("not a position from this list")
    return parsed[0], parsed[1]


def _parseable_day(text: str) -> bool:
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


class FakeEngine(BaseHTTPRequestHandler):
    """Bearer auth, slot caps, one scope list, keyset paging, and the engine's errors."""

    scenario = "ok"

    def log_message(self, *_args: str) -> None:  # keep the run quiet
        pass

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        error: dict[str, object] = {"code": code, "message": message}
        if details:
            error["details"] = details
        self._send(status, {"error": error})

    def _record(self, method: str) -> dict:
        parsed = urlparse(self.path)
        entry = {
            "method": method,
            "path": parsed.path,
            "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
            "authorization": self.headers.get("authorization"),
            "scenario": FakeEngine.scenario,
        }
        REQUEST_LOG.append(entry)
        return entry

    # Every write verb on a read path answers 405 `read_only`, the way
    # v1Read.on(["POST","PUT","PATCH","DELETE"], ...) does. A bare 404 would
    # suggest the path does not exist; it does, it is simply not writable (F-V16).
    def _write_door(self) -> None:
        self._record(self.command)
        path = urlparse(self.path).path
        if path in READ_ONLY_DOOR or re.fullmatch(r"/v1/leads/[^/]+", path):
            self._error(
                405,
                "read_only",
                "this /v1 route is read-only; settings writes stay on /api/settings",
            )
        else:
            self._error(404, "not_found", f"no route {path}")

    do_POST = _write_door
    do_PUT = _write_door
    do_PATCH = _write_door
    do_DELETE = _write_door

    def do_GET(self) -> None:
        self._record("GET")
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if FakeEngine.scenario == "no_service_tokens":
            self._error(
                503,
                "service_auth_not_configured",
                "SERVICE_TOKENS is not set or not a valid token list; refusing /v1",
            )
            return

        bearer = (self.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not bearer:
            self._error(401, "invalid_token", "bearer token required")
            return
        caps = TOKEN_CAPS.get(bearer)
        if caps is None:
            self._error(401, "invalid_token", "invalid token")
            return

        scope = query.get("account_scope", "")
        if not scope or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", scope):
            self._error(400, "invalid_account_scope", "account_scope must be a slug of 1-64 chars")
            return
        if scope not in SCOPES:
            # The engine names neither the token's scopes nor the requested one.
            self._error(403, "wrong_scope", "this token may not serve the requested account_scope")
            return
        if "read" not in caps:
            self._error(403, "insufficient_capability", "this token may not read on /v1")
            return

        routes = {
            "/v1/leads": lambda: self._list(query, scope, queued=False),
            "/v1/review-queue": lambda: self._list(query, scope, queued=True),
            "/v1/metrics": lambda: self._metrics(query, scope),
            "/v1/settings": lambda: self._send(200, self._settings(scope)),
        }
        handler = routes.get(parsed.path)
        if handler:
            handler()
            return
        single = re.fullmatch(r"/v1/leads/([^/]+)", parsed.path)
        if single:
            self._one(unquote(single.group(1)), scope)
            return
        verdict = re.fullmatch(r"/v1/leads/([^/]+)/verdict", parsed.path)
        if verdict:
            self._verdict(unquote(verdict.group(1)), scope)
            return
        self._error(404, "not_found", f"no route {parsed.path}")

    # ---- the list routes: one keyset page, no offset (F-V14) --------------------

    def _list(self, query: dict, scope: str, queued: bool) -> None:
        bad = [name for name, value in self._query_problems(query)]
        if bad:
            self._error(400, "invalid_query", "query parameter not understood", {"params": bad})
            return
        if query.get("cursor"):
            try:
                _decode_cursor(query["cursor"])
            except ValueError:
                self._error(
                    400,
                    "invalid_query",
                    "cursor is not a position from this list",
                    {"params": ["cursor"]},
                )
                return

        limit = int(query["limit"]) if query.get("limit", "").isdigit() else DEFAULT_PAGE
        rows = LEAD_ROWS if queued else self._filtered(query)
        if queued:
            rows = [row for row in rows if row["status"] == "needs_human"]
        if query.get("cursor"):
            at, lead_id = _decode_cursor(query["cursor"])
            rows = [
                row
                for row in rows
                if (row["updated_at"], LEADS[row["external_id"]]["lead_id"]) < (at, lead_id)
            ]
        peeked = rows[: limit + 1]
        page = peeked[:limit]
        last = page[-1] if page else None
        self._send(
            200,
            {
                "account_scope": scope,
                "leads": page,
                "next_cursor": (
                    _encode_cursor(last["updated_at"], LEADS[last["external_id"]]["lead_id"])
                    if len(peeked) > limit and last
                    else None
                ),
            },
        )

    def _query_problems(self, query: dict) -> list[tuple[str, str]]:
        """The ListQuerySchema failures, as (parameter, reason) pairs."""
        problems: list[tuple[str, str]] = []
        if query.get("status", "") and query["status"] not in STATUSES:
            problems.append(("status", "unknown status"))
        if query.get("verdict", "") and query["verdict"] not in VERDICT_FILTERS:
            problems.append(("verdict", "unknown verdict"))
        if query.get("tier", "") and query["tier"] not in TIERS:
            problems.append(("tier", "unknown tier"))
        limit = query.get("limit", "")
        if limit and (not limit.isdigit() or not 1 <= int(limit) <= MAX_PAGE):
            problems.append(("limit", f"an integer from 1 to {MAX_PAGE}"))
        cursor = query.get("cursor", "")
        if len(cursor) > 400:
            problems.append(("cursor", "at most 400 chars"))
        return problems

    def _filtered(self, query: dict) -> list:
        return [
            row for row in LEAD_ROWS if not query.get("status") or row["status"] == query["status"]
        ]

    # ---- one lead ---------------------------------------------------------------

    def _one(self, external_id: str, scope: str) -> None:
        row = LEADS.get(external_id)
        if row is None:
            self._error(404, "lead_not_found", "no lead with that external_id in this scope")
            return
        self._send(
            200,
            {
                **lead_view(row),
                "thresholds": THRESHOLDS,
                "readings": READINGS[external_id],
            },
        )

    def _verdict(self, external_id: str, scope: str) -> None:
        row = LEADS.get(external_id)
        if row is None:
            self._error(404, "lead_not_found", f"no verdict for {external_id} in {scope}")
            return
        self._send(
            200,
            {
                "external_id": external_id,
                "account_scope": scope,
                "verdict": row["label"] or row["verdict"],
                "probability_qualified": row["probability_qualified"],
                "confidence": row["confidence"],
                "stage": "human" if row["label"] else row["stage"],
                "transcript_hash": row["transcript_hash"],
                "thresholds": THRESHOLDS,
                "label": None,
            },
        )

    # ---- metrics and settings ---------------------------------------------------

    def _metrics(self, query: dict, scope: str) -> None:
        name = query.get("window") or "7d"
        days = WINDOWS.get(name)
        if days is None and name != "all":
            self._error(
                400,
                "invalid_query",
                f"window must be one of {', '.join(WINDOWS)}",
                {"params": ["window"]},
            )
            return
        # One clock read for every count in the answer, and the answer says what
        # it covered (F-V20).
        until_at = datetime(2026, 10, 21)
        until = until_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        since = (
            None
            if name == "all"
            else (until_at - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        )
        self._send(
            200,
            {
                "account_scope": scope,
                "window": name,
                "since": since,
                "until": until,
                "thresholds": THRESHOLDS,
                "leads": 2,
                "by_status": {"final": 1, "needs_human": 1},
                "by_verdict": {"qualified": 1, "cannot_tell": 1},
                "by_tier": {"hot": 1, "review": 1},
                "review_queue": 1,
                "reviews": {
                    "total": 1,
                    "by_label": {"qualified": 1},
                    "per_day": [{"day": "2026-10-20", "count": 1}],
                },
                "agreement": {
                    "clef_vs_human": {"compared": 1, "agree": 1, "rate": 1.0},
                    "glm_vs_human": {"compared": 0, "agree": 0, "rate": None},
                },
            },
        )

    def _settings(self, scope: str) -> dict:
        clef = THRESHOLDS["clef"]
        return {
            "account_scope": scope,
            "thresholds": THRESHOLDS,
            "tiers": {
                "hot": f"verdict qualified with P(qualified) at or above the Clef threshold ({clef})",
                "warm": "verdict qualified below the Clef threshold",
                "cold": "verdict not_qualified",
                "review": "no verdict yet: the case is queued, pending, or the models abstained",
            },
            "models": {"clef": "clef-1", "glm": "glm-1"},
            "example_count": 6,
            "timeouts_ms": {"clef": 20000, "glm": 60000},
            "documents": {"icp": 3, "scoring_guide": 2},
            "writable_via": "PATCH /api/settings (reviewer password); /v1 is read-only",
        }


# ---- driving the CLI -------------------------------------------------------------


def run_cli(argv: list[str], env: dict[str, str], tool_dir: Path = TOOL_DIR) -> dict:
    environ = {
        **os.environ,
        "QWALO_ENGINE_URL": "",
        "QWALO_ACCOUNT_SCOPE": "",
        "QWALO_ENGINE_TOKEN": "",
        # Cleared, or a developer's shell makes every case "configured" and the
        # unconfigured-link assertion in seller_text_stays_behind_the_link would lie.
        "QWALO_CONSOLE_URL": "",
    }
    environ.update(env)
    # --no-cache because uv keys a path source's cached build by path, not by
    # content: without it this gate can pass against a wheel older than the tool it
    # is supposed to be proving. -q keeps uv's build chatter out of stderr, which is
    # the CLI's own output as far as the cases below are concerned.
    proc = subprocess.run(
        ["uvx", "--no-cache", "-q", "--from", str(tool_dir), "engine", *argv],
        capture_output=True,
        text=True,
        env=environ,
        timeout=180,
        check=False,
    )
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        body = {"_stdout": proc.stdout.strip(), "_stderr": proc.stderr.strip()[:400]}
    return {"argv": argv, "exit": proc.returncode, "json": body}


def cli_env(base_url: str, scope: str = "ere", token: str = GOOD_TOKEN) -> dict[str, str]:
    return {
        "QWALO_ENGINE_URL": base_url,
        "QWALO_ACCOUNT_SCOPE": scope,
        "QWALO_ENGINE_TOKEN": token,
    }


CASES: list = []


def case(func):
    CASES.append(func)
    return func


@case
def leads_are_read(base: str) -> dict:
    """One keyset page, in the row shape src/v1/routes.ts leadView() emits."""
    out = run_cli(["leads", "--limit", "5"], cli_env(base))
    body = out["json"]
    rows = body.get("leads", [])
    ids = [row["external_id"] for row in rows]
    newest_first = [row["updated_at"] for row in rows] == sorted(
        (row["updated_at"] for row in rows), reverse=True
    )
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and ids == ["sc-synthetic-0002", "sc-synthetic-0001"]
        and newest_first
        and body.get("account_scope") == "ere"
        and rows != []
        and set(LEAD_ROWS[0]) == set(rows[0]),
        "row_fields": sorted(rows[0]) if rows else [],
        "detail": "row field set matches leadView(), newest first",
    }


@case
def lead_detail_carries_thresholds_and_readings(base: str) -> dict:
    """GET /v1/leads/:id answers the view plus thresholds plus every reading."""
    out = run_cli(["lead", "sc-synthetic-0001"], cli_env(base))
    body = out["json"]
    readings = body.get("readings") or [{}]
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and body.get("external_id") == "sc-synthetic-0001"
        and body.get("thresholds") == THRESHOLDS
        and readings[0].get("decisive") == 1
        and set(LEAD_ROWS[0]) <= set(body),
        "reading_fields": sorted(readings[0]),
    }


@case
def verdict_answer_carries_the_merged_fields(base: str) -> dict:
    out = run_cli(["verdict", "sc-synthetic-0001"], cli_env(base))
    body = out["json"]
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and body.get("verdict") == "qualified"
        and body.get("probability_qualified") == 0.93
        and body.get("thresholds") == THRESHOLDS
        and set(body)
        == {
            "external_id",
            "account_scope",
            "verdict",
            "probability_qualified",
            "confidence",
            "stage",
            "transcript_hash",
            "thresholds",
            "label",
        },
        "fields": sorted(body),
    }


@case
def queue_is_read(base: str) -> dict:
    """The review queue answers with lead rows, not a queue-shaped invention."""
    out = run_cli(["queue", "--limit", "5"], cli_env(base))
    body = out["json"]
    rows = body.get("leads", [])
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and len(rows) == 1
        and rows[0]["status"] == "needs_human"
        and "next_cursor" in body
        and set(rows[0]) == set(LEAD_ROWS[0]),
        "external_id": rows[0]["external_id"] if rows else None,
    }


@case
def metrics_are_read(base: str) -> dict:
    """Metrics name the window they covered, and count what the surface counts."""
    out = run_cli(["metrics"], cli_env(base))
    body = out["json"]
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and body.get("window") == "7d"
        and body.get("since")
        and body.get("until")
        and body.get("thresholds") == THRESHOLDS
        and set(body.get("agreement", {})) == {"clef_vs_human", "glm_vs_human"},
        "counts_seen": {k: body.get(k) for k in ("leads", "review_queue", "by_tier")},
        "detail": "the default window is 7d and the answer says so",
    }


@case
def metrics_window_reaches_the_engine(base: str) -> dict:
    """--window is sent, not swallowed: 30d comes back as a 30-day window."""
    out = run_cli(["metrics", "--window", "30d"], cli_env(base))
    body = out["json"]
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and body.get("window") == "30d"
        and out["argv"][-2:] == ["--window", "30d"],
        "since": body.get("since"),
    }


@case
def bad_metrics_window_is_the_engines_answer(base: str) -> dict:
    """A window the surface does not know comes back as its 400, verbatim."""
    out = run_cli(["metrics", "--window", "yesterday"], cli_env(base))
    return {
        "ran": out,
        "ok": out["exit"] != 0 and out["json"].get("error") == "invalid_query",
        "code": out["json"].get("error"),
        "detail": "the tool does not invent its own validation message",
    }


@case
def page_cursor_round_trips(base: str) -> dict:
    """next_cursor from one page is the cursor that fetches the next one.

    The surface pages by keyset. An agent that ignores the cursor reads the same
    page forever; one that mangles it gets 400 invalid_query, never a full scan.
    """
    first = run_cli(["leads", "--limit", "1"], cli_env(base))
    cursor = first["json"].get("next_cursor")
    if not cursor:
        return {"ok": False, "detail": "limit=1 returned no next_cursor", "ran": first}
    second = run_cli(["leads", "--limit", "1", "--cursor", cursor], cli_env(base))
    rows = second["json"].get("leads", [])
    return {
        "ok": first["exit"] == 0
        and second["exit"] == 0
        and len(rows) == 1
        and rows[0]["external_id"] != first["json"]["leads"][0]["external_id"],
        "first": first["json"]["leads"][0]["external_id"],
        "second": rows[0]["external_id"] if rows else None,
    }


@case
def junk_cursor_is_the_engines_400(base: str) -> dict:
    out = run_cli(["leads", "--cursor", "not-a-position"], cli_env(base))
    return {
        "ran": out,
        "ok": out["exit"] != 0 and out["json"].get("error") == "invalid_query",
        "code": out["json"].get("error"),
    }


@case
def a_token_without_read_cannot_read(base: str) -> dict:
    """The engine's `caps` are the read-only guarantee, and the tool names it.

    A slot with caps ["write"] is a valid SERVICE_TOKENS entry. Reading with it
    answers 403 insufficient_capability (src/v1/auth.ts requireCap): the token is
    wrong for this release, which is not the same message as "no leads".
    """
    env = cli_env(base)
    env["QWALO_ENGINE_TOKEN"] = WRITE_ONLY_TOKEN
    out = run_cli(["leads", "--limit", "5"], env)
    return {
        "ran": out,
        "ok": out["exit"] != 0
        and out["json"].get("error") == "insufficient_capability"
        and out["json"].get("http_status") == 403,
        "code": out["json"].get("error"),
        "detail": "a Qwalo release holds a caps:[read] slot; the reverse is loud",
    }


@case
def bearer_token_is_sent(base: str) -> dict:
    before = len(REQUEST_LOG)
    out = run_cli(["metrics"], cli_env(base))
    seen = REQUEST_LOG[before:]
    return {
        "ran": out,
        "ok": all(r["authorization"] == f"Bearer {GOOD_TOKEN}" for r in seen) and out["exit"] == 0,
        "detail": f"{len(seen)} request(s) carried the bearer",
    }


@case
def scope_is_forced_by_env(base: str) -> dict:
    before = len(REQUEST_LOG)
    out = run_cli(["leads"], cli_env(base, scope="ere"))
    flagged = run_cli(["leads", "--account-scope", "other"], cli_env(base, scope="ere"))
    seen = REQUEST_LOG[before:]
    # Every request carries the env scope, and the CLI refuses a scope argument,
    # so an agent cannot name another tenant (`FAILURE_MODES.md` F1).
    ok = (
        out["exit"] == 0
        and all(r["query"].get("account_scope") == "ere" for r in seen)
        and flagged["exit"] != 0
        and "unrecognized" in flagged["json"].get("_stderr", "").lower()
    )
    return {"ran": {"accepted": out, "rejected": flagged}, "ok": ok}


@case
def cross_scope_request_is_refused(base: str) -> dict:
    out = run_cli(["leads"], cli_env(base, scope="acme"))
    body = out["json"]
    return {
        "ran": out,
        "ok": out["exit"] != 0
        and body.get("error") == "wrong_scope"
        and body.get("http_status") == 403,
        "detail": "a token held for ere answers 403 for another client, and the tool says so",
    }


@case
def sandbox_shaped_env_sends_no_bearer(base: str) -> dict:
    """The real sandbox shape: no token variable exists, and none is invented.

    Inject mode seeds no placeholder env var (placeholder_env() covers
    replace-mode secrets only), so in a sandbox QWALO_ENGINE_TOKEN is absent
    and iron-proxy adds the bearer in flight. The tool's job is to send no
    Authorization header rather than a made-up one (F2, F4).
    """
    before = len(REQUEST_LOG)
    out = run_cli(["metrics"], {"QWALO_ENGINE_URL": base, "QWALO_ACCOUNT_SCOPE": "ere"})
    seen = REQUEST_LOG[before:]
    ok = (
        all(r["authorization"] is None for r in seen)
        and out["exit"] != 0
        and out["json"].get("error") == "invalid_token"
    )
    return {"ran": out, "requests": seen, "ok": ok}


@case
def placeholder_token_is_never_sent(base: str) -> dict:
    before = len(REQUEST_LOG)
    out = run_cli(["metrics"], cli_env(base, token="QWALO_ENGINE_TOKEN"))
    seen = REQUEST_LOG[before:]
    # In a sandbox iron-proxy adds the real bearer; a bare placeholder must not
    # go on the wire as if it were a credential (F2, F4).
    ok = (
        all(r["authorization"] is None for r in seen)
        and out["exit"] != 0
        and out["json"].get("error") == "invalid_token"
    )
    return {"ran": out, "requests": seen, "ok": ok}


@case
def invalid_token_is_reported(base: str) -> dict:
    out = run_cli(["metrics"], cli_env(base, token="expired-e2e-token"))
    return {
        "ran": out,
        "ok": out["exit"] != 0
        and out["json"].get("error") == "invalid_token"
        and out["json"].get("http_status") == 401,
    }


@case
def engine_503_is_reported(base: str) -> dict:
    FakeEngine.scenario = "no_service_tokens"
    try:
        out = run_cli(["metrics"], cli_env(base))
    finally:
        FakeEngine.scenario = "ok"
    return {
        "ran": out,
        "ok": out["exit"] != 0 and out["json"].get("error") == "service_auth_not_configured",
        "detail": "an unconfigured engine is named, never read as an empty queue",
    }


@case
def engine_404_is_reported(base: str) -> dict:
    out = run_cli(["verdict", "sc-synthetic-9999"], cli_env(base))
    return {
        "ran": out,
        "ok": out["exit"] != 0 and out["json"].get("error") == "lead_not_found",
    }


@case
def missing_engine_url_fails_closed(base: str) -> dict:
    before = len(REQUEST_LOG)
    out = run_cli(["metrics"], {"QWALO_ACCOUNT_SCOPE": "ere", "QWALO_ENGINE_TOKEN": GOOD_TOKEN})
    return {
        "ran": out,
        "ok": out["exit"] != 0
        and out["json"].get("error") == "engine_url_missing"
        and len(REQUEST_LOG) == before,
        "detail": "no request left the process",
    }


@case
def missing_account_scope_fails_closed(base: str) -> dict:
    before = len(REQUEST_LOG)
    out = run_cli(["metrics"], {"QWALO_ENGINE_URL": base, "QWALO_ENGINE_TOKEN": GOOD_TOKEN})
    return {
        "ran": out,
        "ok": out["exit"] != 0
        and out["json"].get("error") == "account_scope_missing"
        and len(REQUEST_LOG) == before,
    }


@case
def engine_unreachable_is_reported(base: str) -> dict:
    # 127.0.0.1:1 — nothing listens there.
    out = run_cli(["metrics"], cli_env("http://127.0.0.1:1"))
    return {
        "ran": out,
        "ok": out["exit"] != 0 and out["json"].get("error") == "engine_unreachable",
    }


@case
def cli_exposes_no_write_verbs(base: str) -> dict:
    out = run_cli(["--help"], cli_env(base))
    verbs = sorted(re.findall(r"\{([a-z,]+)\}", out["json"].get("_stdout", ""))[0].split(","))
    source = (TOOL_DIR / "client.py").read_text()
    write_calls = re.findall(
        r"method\s*=\s*[\"'](?!GET)|https?\.?(post|put|patch|delete)\(", source
    )
    return {
        "ok": verbs == ["lead", "leads", "metrics", "queue", "verdict"] and not write_calls,
        "verbs": verbs,
        "write_calls": write_calls,
        "detail": "the read surface is the whole surface",
    }


@case
def no_write_request_reaches_the_engine(base: str) -> dict:
    before = len(REQUEST_LOG)
    for argv in (
        ["leads"],
        ["lead", "sc-synthetic-0001"],
        ["verdict", "sc-synthetic-0001"],
        ["queue"],
        ["metrics"],
    ):
        run_cli(argv, cli_env(base))
    methods = {r["method"] for r in REQUEST_LOG[before:]}
    return {"ok": methods == {"GET"}, "methods": sorted(methods)}


@case
def mutation_proves_the_placeholder_guard_bites(base: str) -> dict:
    """Delete the guard and the leak must appear, or the suite is watching nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "engine"
        shutil.copytree(TOOL_DIR, copy)
        source = (copy / "client.py").read_text()
        mutated = source.replace("if self.token and self.token != PLACEHOLDER:", "if self.token:")
        if mutated == source:
            return {
                "ok": False,
                "detail": "the guard is no longer where client.py documents it",
            }
        (copy / "client.py").write_text(mutated)
        before = len(REQUEST_LOG)
        run_cli(["metrics"], cli_env(base, token=PLACEHOLDER), tool_dir=copy)
        seen = REQUEST_LOG[before:]
    leaked = [r["authorization"] for r in seen if r["authorization"]]
    return {
        "ok": leaked == [f"Bearer {PLACEHOLDER}"] and PLACEHOLDER != GOOD_TOKEN,
        "mutated_tool_sent": leaked,
        "detail": "the placeholder reaches the wire only when the guard is removed",
    }


@case
def values_template_renders_in_the_real_chart(base: str) -> dict:
    """The values the shipping script writes must survive the real chart.

    Rendered through deploy-client.sh --values, not through a copy of the
    substitution written inside this test. A test that re-implements the render can
    pass while the deploy is broken.
    """
    if not shutil.which("helm"):
        return {
            "ok": True,
            "skipped": True,
            "detail": "helm is not installed here; run this suite where it is",
        }
    repo = OVERLAY_DIR.parents[1]
    baked = re.search(
        r"postgresql://[^@\s]+@([A-Za-z0-9._-]+):\d+",
        (repo / "contrib" / "scripts" / "bootstrap-k8s-secrets.sh").read_text(),
    )
    with tempfile.TemporaryDirectory() as tmp:
        values = Path(tmp) / "values.ere.yaml"
        rendered_values = run_script(
            [str(DEPLOY_DIR / "deploy-client.sh"), "--values", "ere", str(values)]
        )
        if rendered_values["exit"] != 0:
            return {
                "ok": False,
                "detail": "the shipping render failed",
                "stderr": rendered_values["stderr"][-400:],
            }
        text = values.read_text()
        left = re.findall(r"<<[A-Z_]+>>", text)
        if left:
            return {"ok": False, "detail": f"placeholders survived the render: {left}"}
        proc = subprocess.run(
            [
                "helm",
                "template",
                # The release name the deploy script uses, not the client name: the
                # Postgres host in centaur-infra-env is built from it.
                "centaur",
                str(repo / "contrib" / "chart"),
                "-f",
                str(repo / "contrib" / "chart" / "values.yaml"),
                "-f",
                str(values),
                "--set-file",
                f"overlay.systemPrompt={OVERLAY_DIR / 'services/sandbox/SYSTEM_PROMPT.md'}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            if "missing in charts/" in proc.stderr:
                return {
                    "ok": True,
                    "skipped": True,
                    "detail": "chart dependencies absent; run: helm dependency build contrib/chart",
                }
            return {"ok": False, "detail": proc.stderr[-600:]}
        rendered = proc.stdout
    prompt_line = (OVERLAY_DIR / "services/sandbox/SYSTEM_PROMPT.md").read_text().splitlines()[0]

    def scoped(line_text: str, value: str) -> bool:
        lines = [line for line in rendered.splitlines() if line_text in line]
        return bool(lines) and all(value in line for line in lines)

    # WORKFLOW_DIRS is emitted twice: the chart's built-in (the base repo's workflows)
    # and this overlay's override. A container takes the last definition of a name, so
    # order is the control and the last value is the effective one.
    workflow_dirs = re.findall(r'name: WORKFLOW_DIRS\n\s*value: "([^"]+)"', rendered)
    want = {
        "tool dirs carry the overlay tools": "overlays/qwalo/tools" in rendered,
        "engine url reaches the sandbox with its host": scoped(
            "QWALO_ENGINE_URL", "qwalo.raava.dev"
        ),
        "engine scope reaches the sandbox": scoped("QWALO_ACCOUNT_SCOPE", '\\"ere\\"'),
        "console link reaches the sandbox": scoped("QWALO_CONSOLE_URL", "qwalo.raava.dev"),
        "one host per client": 'host: "ere.qwalo.raava.dev"' in rendered,
        "system prompt is rendered in": prompt_line in rendered,
        "no literal slack secret": "SLACK_BOT_TOKEN" not in rendered.split("secretKeyRef")[0],
        # The Service the baked DATABASE_URL points at must be the Service the chart
        # creates, or the release boots with no database.
        "postgres service matches the baked host": baked is not None
        and f"name: {baked.group(1)}" in rendered,
        "webhook source namespace is admitted": "kubernetes.io/metadata.name: kube-system"
        in rendered,
        "harness auth is api_key": re.search(
            r'name: CODEX_AUTH_MODE\n\s*value: "api_key"', rendered
        )
        is not None,
        "base workflow tree is hidden": len(workflow_dirs) == 2
        and workflow_dirs[0].endswith("/workflows")
        and "overlays" not in workflow_dirs[0]
        and workflow_dirs[-1].endswith("/overlays/qwalo/workflows"),
        # Spans carry prompts and tool output, which is the seller's text. A client
        # release exports nothing: no endpoint, so nothing to route (F12).
        "no trace sink is configured": "OTEL" not in rendered,
        "workflow_dirs_seen": workflow_dirs,
    }
    return {
        "ok": all(want.values()),
        "assertions": want,
        "resources": rendered.count("\nkind:"),
    }


@case
def only_the_engine_tool_ships(base: str) -> dict:
    shipped = sorted(
        d.name for d in (OVERLAY_DIR / "tools").iterdir() if d.is_dir() and d.name != "personas"
    )
    return {"ok": shipped == ["engine"], "shipped_tools": shipped}


@case
def the_sandbox_gets_only_the_overlay_tool_tree(base: str) -> dict:
    """`subdir: tools` would hand a sandbox telegram, discord and twitter.

    What matters is what the release points the tools-bootstrap container
    at. The base tree has a working tools/comms/telegram send_message, so
    mounting it would contradict the no-send claim outright (F3).
    """
    template = (DEPLOY_DIR / "values.client.yaml.tmpl").read_text()
    block = template.split("toolServer:", 1)[1].split("\n\n", 1)[0]
    subdirs = [line.strip() for line in block.splitlines() if line.strip().startswith("subdir:")]
    telegram = REPO_DIR / "tools" / "comms" / "telegram" / "client.py"
    if not telegram.exists():
        return {
            "ok": False,
            "detail": f"{telegram.relative_to(REPO_DIR)} is absent, so this checkout cannot prove the base tree can send. "
            "Run from a full checkout, or with overlays/qwalo/e2e/run-on-proxmox.sh, which syncs that path.",
        }
    base_can_send = "send_message" in telegram.read_text()
    return {
        "ok": (
            subdirs == ["subdir: overlays/qwalo/tools"]
            and "extraSources: []" in block
            and base_can_send
        ),
        "tool_server_block": block.strip(),
        "base_telegram_can_send": base_can_send,
        "detail": "the base tool tree can message people, so it must not be mounted",
    }


@case
def deploy_script_rejects_bad_client(base: str) -> dict:
    bad = run_script([str(DEPLOY_DIR / "deploy-client.sh"), "--check", "ERE_Investors"])
    good = run_script([str(DEPLOY_DIR / "deploy-client.sh"), "--check", "ere"])
    return {
        "ok": bad["exit"] == 2 and good["exit"] == 0,
        "rejected": bad,
        "accepted": good["stdout"][-400:],
    }


PINNED = {"QWALO_IMAGE_TAG": "2026.10.20-d441128", "QWALO_TOOL_REF": "d441128e"}


def run_script(argv: list[str], env: dict[str, str] | None = None) -> dict:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **PINNED, **(env or {})},
    )
    # Recorded argv is repo-relative. A worktree path is machine-specific, and an
    # artifact that embeds one is not reproducible and not portable.
    prefix = str(REPO_DIR) + os.sep
    return {
        "argv": [str(a).replace(prefix, "") for a in argv],
        "exit": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


@case
def manifest_names_only_this_client_host(base: str) -> dict:
    text = (DEPLOY_DIR / "slack-app-manifest.json.tmpl").read_text()
    manifest = json.loads(
        text.replace("<<CLIENT>>", "ere").replace("<<HOST>>", "ere.qwalo.raava.dev")
    )
    url = manifest["settings"]["event_subscriptions"]["request_url"]
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    return {
        "ok": url == "https://ere.qwalo.raava.dev/api/webhooks/slack"
        and "chat:write.public" not in scopes
        and manifest["settings"]["event_subscriptions"]["bot_events"] == ["app_mention"],
        "request_url": url,
        "detail": "one app, one workspace, one host; no shared router, no unprompted posting scope",
    }


@case
def values_template_has_no_secret_values(base: str) -> dict:
    template = (DEPLOY_DIR / "values.client.yaml.tmpl").read_text()
    script = (DEPLOY_DIR / "deploy-client.sh").read_text()
    offenders = [
        line
        for line in template.splitlines()
        if re.match(
            r"^[^#]*[A-Z_]*(BOT_TOKEN|SIGNING_SECRET|API_KEY|ENGINE_TOKEN|PASSWORD)[A-Z_]*\s*:",
            line,
        )
    ]
    # Every secret the script touches arrives from an `op read` of a vault named
    # after the client, so one client's vault cannot fund another client's release.
    vault_line = 'VAULT="prod-qwalo-${CLIENT}"'
    reads = re.findall(r'op read "([^"]+)"', script)
    literal_secrets = [
        line.strip()
        for line in script.splitlines()
        if re.match(r"^(export\s+)?[A-Z_]*(TOKEN|SECRET|PASSWORD)[A-Z_]*=", line.strip())
    ]
    leaked = [line for line in literal_secrets if "op read" not in line]
    # iron-proxy resolves these two itself, so the script must not hold them
    proxy_only = [
        line
        for line in script.splitlines()
        if re.match(r"^export (OPENAI_API_KEY|QWALO_ENGINE_TOKEN)=", line.strip())
    ]
    return {
        "ok": not offenders
        and not leaked
        and not proxy_only
        and vault_line in script
        and len(reads) == 4
        and all(ref.startswith("op://${VAULT}/") for ref in reads)
        # the existence check runs over this list, so the engine token's ref is
        # the proxy's own shape: op://$OP_VAULT/QWALO_ENGINE_TOKEN/credential
        and "QWALO_ENGINE_TOKEN" in re.search(r"VAULT_ITEMS=\(([^)]*)\)", script).group(1),
        "offenders": offenders,
        "literal_secret_assignments": leaked,
        "exported_proxy_secrets": proxy_only,
        "op_reads": reads,
    }


@case
def account_scope_cannot_be_overridden(base: str) -> dict:
    script = (DEPLOY_DIR / "deploy-client.sh").read_text()
    lines = [line for line in script.splitlines() if line.startswith("ENGINE_SCOPE=")]
    return {
        "ok": lines == ['ENGINE_SCOPE="$CLIENT"'],
        "assignments": lines,
        "detail": "one assignment, no override: a stale export cannot retarget a client",
    }


@case
def deploy_requires_pinned_release_identity(base: str) -> dict:
    script = str(DEPLOY_DIR / "deploy-client.sh")
    unpinned = run_script([script, "--check", "ere"], {"QWALO_IMAGE_TAG": "", "QWALO_TOOL_REF": ""})
    floating_tag = run_script([script, "--check", "ere"], {"QWALO_IMAGE_TAG": "latest"})
    moving_ref = run_script([script, "--check", "ere"], {"QWALO_TOOL_REF": "main"})
    pinned = run_script([script, "--check", "ere"])
    return {
        "ok": all(r["exit"] != 0 for r in (unpinned, floating_tag, moving_ref))
        and pinned["exit"] == 0,
        "unpinned": unpinned["stderr"].strip(),
        "floating_tag": floating_tag["stderr"].strip(),
        "moving_ref": moving_ref["stderr"].strip(),
    }


@case
def engine_secret_ref_matches_the_proxy_lookup(base: str) -> dict:
    """iron-proxy resolves op://$OP_VAULT/<SECRET_NAME>/credential, never /password.

    Source: centaur-iron-control registry.rs builds that ref from the
    placeholder name. A vault item named for prose, or a field named password,
    never resolves and the release answers 401 forever.
    """
    script = (DEPLOY_DIR / "deploy-client.sh").read_text()
    refs = re.findall(r"op://\$\{VAULT\}/([A-Za-z_{}$]+)/([a-z]+)", script)
    items = re.search(r"VAULT_ITEMS=\(([^)]*)\)", script)
    return {
        "ok": bool(items)
        and "QWALO_ENGINE_TOKEN" in items.group(1)
        and {field for _, field in refs} == {"credential"}
        and "password" not in script,
        "refs": sorted(set(refs)),
        "vault_items": items.group(1).split() if items else [],
    }


@case
def no_double_scheme_url_anywhere(base: str) -> dict:
    """No `https://://` typo in any source file of the overlay.

    Source suffixes only: a compiled cache or a binary has no URLs to typo, and
    reporting one as an offender hides the real list.
    """
    sources = {".py", ".md", ".sh", ".yaml", ".yml", ".toml", ".tmpl", ".json", ".txt", ".ts"}
    offenders = []
    for path in sorted(p for p in OVERLAY_DIR.rglob("*") if p.is_file() and p.suffix in sources):
        # This file builds the bad string from pieces in order to look for it, so it
        # matches itself. Nothing else in the overlay may contain it.
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(errors="ignore")
        if "https://" + "://" in text or "http:" + "//" + "://" in text:
            offenders.append(str(path.relative_to(OVERLAY_DIR)))
    return {"ok": not offenders, "offenders": offenders}


@case
def deploy_script_gates_the_apply(base: str) -> dict:
    """Nothing is applied until identity, vault scope, and secret refs are proven.

    Located by the calls themselves, not by the words: these names also appear in
    comments and in the usage text, and a comment cannot gate anything.
    """
    script = (DEPLOY_DIR / "deploy-client.sh").read_text()
    markers = {
        "pinned_identity": "check_pinned\n",
        "vault_scope": "|| check_vault_scope\n",
        "secret_exists": '|| fail "cannot read op://',
        "infra_secret": '"$REPO_DIR/contrib/scripts/bootstrap-k8s-secrets.sh" --namespace',
        "apply": "helm upgrade --install",
    }
    positions = {}
    for name, needle in markers.items():
        assert needle in script, f"deploy-client.sh no longer contains {name}: {needle!r}"
        positions[name] = script.rindex(needle)
    sequence = ["pinned_identity", "vault_scope", "secret_exists", "infra_secret", "apply"]
    order = [positions[name] for name in sequence]
    return {
        "ok": order == sorted(order) and "set -euo pipefail" in script and "mktemp -d" in script,
        "order": sequence,
        "detail": "identity, vault scope, and secret existence are all proven before the apply",
    }


@case
def vault_scope_gate_passes_a_single_vault(base: str) -> dict:
    """A token that sees only this client's vault is reported clean, not warned.

    The first version could never report clean. Under `set -o pipefail`, a `grep -v`
    that selects nothing exits 1, so a correctly scoped token fell into the
    cannot-check branch and printed a warning while still exiting 0.
    """
    with _stub_op('[{"name":"prod-qwalo-ere"}]') as bindir:
        r = run_script(
            [str(DEPLOY_DIR / "deploy-client.sh"), "--vault-scope", "ere"],
            {"PATH": f"{bindir}:{os.environ['PATH']}"},
        )
    return {
        "ok": r["exit"] == 0 and "vault scope : prod-qwalo-ere only" in r["stdout"],
        "exit": r["exit"],
        "stdout": r["stdout"].strip(),
        "stderr": r["stderr"].strip(),
    }


@contextlib.contextmanager
def _stub_op(vault_json: str | None, exit_code: int = 0):
    """Put a fake `op` first on PATH, so a gate is tested by running it."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        stub = bindir / "op"
        if vault_json is None:
            stub.write_text(f"#!/bin/sh\nexit {exit_code}\n")
        else:
            stub.write_text("#!/bin/sh\ncat <<'JSON'\n" + vault_json + "\nJSON\n")
        stub.chmod(0o755)
        yield bindir


@case
def vault_scope_gate_refuses_a_second_vault(base: str) -> dict:
    """F6 by behaviour: a token that can read another client's vault cannot deploy."""
    with _stub_op('[{"name":"prod-qwalo-ere"},{"name":"prod-qwalo-acme"}]') as bindir:
        r = run_script(
            [str(DEPLOY_DIR / "deploy-client.sh"), "--vault-scope", "ere"],
            {"PATH": f"{bindir}:{os.environ['PATH']}"},
        )
    combined = r["stdout"] + r["stderr"]
    return {
        "ok": r["exit"] != 0 and "prod-qwalo-acme" in combined,
        "exit": r["exit"],
        "names_the_other_vault": "prod-qwalo-acme" in combined,
    }


@case
def vault_scope_gate_fails_closed_when_it_cannot_check(base: str) -> dict:
    """If `op vault list` fails, the gate refuses. Warn-and-continue would deploy on
    the assumption that the token is scoped, which is the whole point of F6."""
    script = str(DEPLOY_DIR / "deploy-client.sh")
    with _stub_op(None, exit_code=1) as bindir:
        path = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        r = run_script([script, "--vault-scope", "ere"], path)
        attested = run_script([script, "--vault-scope", "ere", "--vault-scope-verified"], path)
    return {
        "ok": r["exit"] != 0
        and "--vault-scope-verified" in (r["stdout"] + r["stderr"])
        and attested["exit"] == 0,
        "exit": r["exit"],
        "attested_exit": attested["exit"],
    }


@case
def manifest_mode_writes_the_file_it_promises(base: str) -> dict:
    """`--manifest <client> [file]` honours the optional file argument.

    The argument loop shifts everything it reads, so reading `$1` after the loop is
    always empty: the documented file form printed to stdout and wrote nothing.
    """
    script = str(DEPLOY_DIR / "deploy-client.sh")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "manifest.json"
        r = run_script([script, "--manifest", "ere", str(out)])
        written = out.read_text() if out.exists() else ""
    return {
        "ok": r["exit"] == 0 and written != "" and "ere.qwalo.raava.dev" in written,
        "exit": r["exit"],
        "file_written": written != "",
    }


@case
def release_name_keeps_the_database_host(base: str) -> dict:
    """The Helm release stays `centaur`, or the API is born pointing at no database.

    contrib/scripts/bootstrap-k8s-secrets.sh bakes DATABASE_URL with a host of
    <release>-<chart>-postgres. Naming the release after the client invents a host
    nobody creates, and every sandbox in that namespace fails on its first tool call.
    """
    bootstrap = (REPO_DIR / "contrib" / "scripts" / "bootstrap-k8s-secrets.sh").read_text()
    baked = re.search(r"postgresql://[^@\s]+@([A-Za-z0-9._-]+):\d+", bootstrap)
    chart_name = re.search(
        r"^name:\s*(\S+)", (REPO_DIR / "contrib" / "chart" / "Chart.yaml").read_text(), re.M
    )
    r = run_script([str(DEPLOY_DIR / "deploy-client.sh"), "--check", "ere"])
    release = re.search(r"^helm release\s*:\s*(\S+)", r["stdout"], re.M)
    if not (baked and chart_name and release):
        return {"ok": False, "detail": "missing baked host, chart name, or release name"}
    derived = f"{release.group(1)}-{chart_name.group(1)}-postgres"
    return {
        "ok": derived == baked.group(1),
        "baked_host": baked.group(1),
        "derived_host": derived,
        "helm_release": release.group(1),
    }


@case
def check_reports_the_deploy_facts(base: str) -> dict:
    """--check prints everything a human needs before a deploy, in one place."""
    r = run_script([str(DEPLOY_DIR / "deploy-client.sh"), "--check", "ere"])
    missing = [
        w
        for w in (
            "ere.qwalo.raava.dev",
            PINNED["QWALO_IMAGE_TAG"],
            PINNED["QWALO_TOOL_REF"],
            "prod-qwalo-ere",
            "centaur",
            # The link the agent hands a reviewer is a deploy fact, not a guess.
            "#/review?id=",
        )
        if w not in r["stdout"]
    ]
    return {"ok": r["exit"] == 0 and not missing, "exit": r["exit"], "missing": missing}


@case
def values_template_names_the_webhook_source_namespace(base: str) -> dict:
    """A client release must say where Slack's webhook comes from.

    The chart is deny-by-default and admits only networkPolicy.ingressControllerNamespaces,
    which defaults to kube-system. A tunnel or controller in any other namespace installs
    a release that is Ready, healthy, and deaf: the event POST is dropped and nothing in
    the cluster says why.
    """
    values = (DEPLOY_DIR / "values.client.yaml.tmpl").read_text()
    block = values[values.index("networkPolicy:") :]
    admitted = re.findall(r"^\s{4}-\s+(\S+)", block[:400], re.M)
    return {
        "ok": "ingressControllerNamespaces" in block and len(admitted) >= 1,
        "admitted_namespaces": admitted,
        "detail": "the values file states the admitted source namespace explicitly",
    }


@case
def page_size_cannot_crash_the_tool(base: str) -> dict:
    """`_clamped` is total, because the generic tool-call path has no argument types.

    `cli.py` declares --limit as an int, but api-rs builds the call from a JSON body.
    A limit of "many" must not surface as a traceback: F4 promises a named error or a
    sane page, and this is the one place the tool used to be able to break that.
    """
    probe = (
        "import sys;sys.path.insert(0, sys.argv[1]);"
        "from client import _clamped;"
        "print([_clamped(v, 1, 100) for v in ('many', '50', 1000, None, 0, -5)])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(TOOL_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    got = proc.stdout.strip()
    return {
        "ok": proc.returncode == 0 and got == "[1, 50, 100, 1, 1, 1]",
        "exit": proc.returncode,
        "values": got,
        "stderr": proc.stderr.strip()[-200:],
    }


@case
def lead_lookup_writes_nothing_to_a_database(base: str) -> dict:
    """The owner's rule is that Centaur stores no lead data, so prove it by tripwire.

    Grepping for a write would only prove nobody wrote one today. Instead the tool
    runs with DATABASE_URL, PGPASSWORD, PGHOST and PGPORT pointed at a closed local
    port: anything that opened a Postgres connection would die on the connection, and
    a stateless read answers the lead from the engine and never notices. A non-GET
    request in the same window would be a second story, so it is asserted too.
    """
    env = cli_env(base)
    env.update(
        {
            "DATABASE_URL": "postgresql://nobody:nobody@127.0.0.1:1/ai_v2",
            "PGPASSWORD": "not-a-real-password",
            "PGHOST": "127.0.0.1",
            "PGPORT": "1",
        }
    )
    # The tripwire must be live, or the case proves nothing: if that port accepted a
    # connection, "it never noticed" would be indistinguishable from "it wrote there".
    probe = socket.socket()
    probe.settimeout(2)
    try:
        probe.connect(("127.0.0.1", 1))
        tripwire_live = False
    except OSError:
        tripwire_live = True
    finally:
        probe.close()

    before = len(REQUEST_LOG)
    out = run_cli(["lead", "sc-synthetic-0001"], env)
    seen = REQUEST_LOG[before:]
    return {
        "tripwire_port_closed": tripwire_live,
        "ok": tripwire_live
        and out["exit"] == 0
        and out["json"].get("external_id") == "sc-synthetic-0001"
        and seen != []
        and all(r["method"] == "GET" for r in seen),
        "exit": out["exit"],
        "requests": [{"method": r["method"], "path": r["path"]} for r in seen],
        "detail": "the lead arrived by GET from the engine while no database was reachable",
    }


@case
def overlay_ships_no_database_writer(base: str) -> dict:
    """No tool, workflow, or dependency in the overlay could put a row in a client DB.

    Three parts, because the claim spans three kinds of artifact: the tool declares no
    dependency at all, so no driver is ever installed into a sandbox image; no overlay
    source imports a driver or names a write statement; and the workflow directory a
    client release registers (FAILURE_MODES F10) ships no Python file, so the release
    can register nothing that writes.
    """
    drivers = ("psycopg", "asyncpg", "sqlalchemy", "peewee", "sqlite3", "import dolt")
    writes = ("insert into", "upsert_messages", "create table", "update leads")
    offenders: dict[str, list[str]] = {}
    for path in sorted(OVERLAY_DIR.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".toml", ".sh", ".yaml", ".yml"}:
            continue
        # This file names the strings in order to look for them.
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(errors="ignore").lower()
        hits = [needle for needle in drivers + writes if needle in text]
        if hits:
            offenders[str(path.relative_to(OVERLAY_DIR))] = hits

    declared = (TOOL_DIR / "pyproject.toml").read_text()
    deps_line = [line for line in declared.splitlines() if line.startswith("dependencies")]
    ships_no_deps = deps_line == ["dependencies = []"]
    workflows = [f.name for f in (OVERLAY_DIR / "workflows").glob("*.py")]

    return {
        "ok": not offenders and ships_no_deps and workflows == [],
        "offenders": offenders,
        "tool_dependencies": deps_line,
        "workflow_files": workflows,
        "detail": "no driver, no write statement, no registered workflow that could write",
    }


@case
def seller_text_stays_behind_the_link(base: str) -> dict:
    """Seller text sits in the fake's snapshot and appears in no answer (F13, F-V15).

    The owner's rule (2026-10-21): the agent summarises a seller message and links to
    it; it never quotes it, because the reply is stored in the client's database. The
    deterministic half of that is checkable, so it is checked: the marker is provably
    inside what the fake stores, or the assertion proves nothing; no verb's answer
    contains it; the lead answers carry the console link a human follows; and with no
    console configured the tool adds no link rather than inventing a host.
    """
    env = cli_env(base)
    env["QWALO_CONSOLE_URL"] = "https://console.test"
    calls = {
        "leads": ["leads", "--limit", "5"],
        "lead": ["lead", "sc-synthetic-0001"],
        "verdict": ["verdict", "sc-synthetic-0001"],
        "queue": ["queue", "--limit", "5"],
        "metrics": ["metrics", "--window", "30d"],
    }
    bodies = {name: run_cli(argv, env) for name, argv in calls.items()}
    unconfigured = run_cli(["lead", "sc-synthetic-0001"], cli_env(base))

    stored = SELLER_MARKER in json.dumps(SNAPSHOTS)
    leaked = [name for name, out in bodies.items() if SELLER_MARKER in json.dumps(out["json"])]
    lead_link = bodies["lead"]["json"].get("console_url", "")
    return {
        "ok": (
            stored
            and all(out["exit"] == 0 for out in bodies.values())
            and not leaked
            and lead_link == "https://console.test/#/review?id=sc-synthetic-0001"
            and bodies["verdict"]["json"].get("console_url", "").endswith("id=sc-synthetic-0001")
            and "console_url" not in unconfigured["json"]
        ),
        "marker_is_in_the_store": stored,
        "leaked_in": leaked,
        "console_url": lead_link,
        "link_omitted_when_unconfigured": "console_url" not in unconfigured["json"],
        "detail": "the answer summarises and links; the seller's words stay in the engine and the console",
    }


@case
def persona_forbids_quoting_and_gives_the_link(base: str) -> dict:
    """Both instruction files carry the rule, and neither still permits a quotation.

    The persona and the container prompt are loaded by different paths and either can
    be deployed alone, so the rule lives in both. The old persona told the agent that
    "one quoted line" could carry the answer; if that sentence ever comes back the
    persona rule is dead, so it is asserted away rather than trusted to stay away.
    """
    files = {
        "persona": (OVERLAY_DIR / "tools" / "personas" / "qwalo" / "PROMPT.md"),
        "system_prompt": (OVERLAY_DIR / "services" / "sandbox" / "SYSTEM_PROMPT.md"),
    }
    checks = {}
    for name, path in files.items():
        text = path.read_text().lower()
        checks[name] = {
            "forbids_quoting": "never quote" in text,
            "asks_for_a_summary": "summar" in text,
            "points_at_the_record": "console_url" in text or "console" in text,
            "no_quoted_line_permission": "one quoted line" not in text,
        }
    return {"ok": all(all(v.values()) for v in checks.values()), "checks": checks}


@case
def inert_pieces_are_declared(base: str) -> dict:
    readme = (OVERLAY_DIR / "README.md").read_text().lower()
    return {
        "ok": "persona" in readme
        and "systemprompt" in readme.replace(" ", "")
        and "inert" in readme,
        "detail": "the overlay must not claim inert pieces are live",
    }


@case
def fixtures_carry_no_real_contact_data(base: str) -> dict:
    blob = json.dumps({"leads": LEADS, "rows": LEAD_ROWS})
    return {
        "ok": "@" not in blob and "+" not in blob and "sc-synthetic-0001" in blob,
        "detail": "every fixture id is synthetic; no emails or phone numbers in the suite",
    }


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"fake engine on {base}")

    results = []
    for func in CASES:
        REQUEST_LOG.clear()
        FakeEngine.scenario = "ok"
        try:
            outcome = func(base)
        except Exception as exc:  # a crashing case is a failed case, never a pass
            outcome = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(outcome.pop("ok", False))
        results.append(
            {
                "case": func.__name__,
                "passed": ok,
                "requests": list(REQUEST_LOG),
                **outcome,
            }
        )
        print(f"{'PASS' if ok else 'FAIL'}  {func.__name__}")

    server.shutdown()
    passed = sum(1 for r in results if r["passed"])
    skipped = sum(1 for r in results if r.get("skipped"))
    artifact = {
        "suite": "qwalo-overlay-engine-fake-engine",
        # no timestamp, no absolute path, no ephemeral port: two clean runs are
        # byte-identical, so a diff in the artifact means the behaviour changed
        "engine_contract_note": "see README.md Engine contract",
        "engine_contract": (
            "src/v1/routes.ts + src/v1/auth.ts in raava-solutions/qwaelo-ai-engine (main f5a81e1); "
            "read routes per the t-0027 contract, which is not merged yet"
        ),
        "how_it_runs": (
            "uvx --from overlays/qwalo/tools/engine engine <verb>, the same entrypoint "
            "the sandbox CLI shim uses"
        ),
        "release_identity": {
            # What was under proof. A render without a pinned image and tool ref is
            # a description of a release that does not exist.
            "image_tag": PINNED["QWALO_IMAGE_TAG"],
            "tool_ref": PINNED["QWALO_TOOL_REF"],
        },
        "fake_engine": {
            "bearer": "fixture constant GOOD_TOKEN in run_fake_engine.py; synthetic, never a real credential",
            "account_scopes": SCOPES,
            "fixtures": "synthetic only",
        },
        "passed": passed,
        "skipped": skipped,
        "total": len(results),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n"
    # A recorded request may carry the fixture bearer the fake engine accepts. The
    # shape belongs in the artifact; the value does not.
    text = text.replace(GOOD_TOKEN, "<fixture-bearer>")
    # An errno is a fact about the operating system, not about this overlay. macOS
    # says 61 where Linux says 111; recording it makes the artifact differ per host.
    text = re.sub(r"\[Errno \d+\]", "[Errno <os>]", text)
    # The artifact is committed, so it carries no machine paths and no token. A run
    # that would leak either fails here rather than in review.
    leak = re.search(r"(/Users/|/Volumes/|/home/)[^\" ]*", text)
    if leak:
        print(f"FAIL  artifact carries a machine path: {leak.group(0)}", file=sys.stderr)
        return 1
    if GOOD_TOKEN in text:
        print("FAIL  artifact carries the engine token value", file=sys.stderr)
        return 1
    REPORT.write_text(text)
    print(f"\n{passed}/{len(results)} passed — artifact at {REPORT.relative_to(REPO_DIR)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
