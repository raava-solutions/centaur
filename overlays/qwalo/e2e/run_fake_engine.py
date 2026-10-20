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

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
OVERLAY_DIR = E2E_DIR.parent
REPO_DIR = E2E_DIR.parents[2]
TOOL_DIR = OVERLAY_DIR / "tools" / "engine"
DEPLOY_DIR = OVERLAY_DIR / "deploy"
REPORT = E2E_DIR / "reports" / "fake-engine.json"

GOOD_TOKEN = "e2e-read-token"
PLACEHOLDER = (
    "QWALO_ENGINE_TOKEN"  # the value iron-proxy's placeholder_env seeds in the sandbox
)
SCOPES = ["ere"]

# ---- fixtures: synthetic, no real contact data (FAILURE_MODES F7) ---------------
LEADS = {
    "sc-synthetic-0001": {
        "external_id": "sc-synthetic-0001",
        "account_scope": "ere",
        "verdict": "qualified",
        "probability_qualified": 0.93,
        "confidence": 0.93,
        "stage": "clef",
        "transcript_hash": "a" * 64,
        "thresholds": {"clef": 0.85, "glm": 0.8},
        "label": None,
    },
    "sc-synthetic-0002": {
        "external_id": "sc-synthetic-0002",
        "account_scope": "ere",
        "verdict": "cannot_tell",
        "probability_qualified": 0.51,
        "confidence": 0.51,
        "stage": None,
        "transcript_hash": "b" * 64,
        "thresholds": {"clef": 0.85, "glm": 0.8},
        "label": None,
    },
}
LEAD_ROWS = [
    {
        "external_id": "sc-synthetic-0001",
        "verdict": "qualified",
        "stage": "clef",
        "probability_qualified": 0.93,
        "updated_at": "2026-10-20T00:00:00Z",
    },
    {
        "external_id": "sc-synthetic-0002",
        "verdict": "cannot_tell",
        "stage": None,
        "probability_qualified": 0.51,
        "updated_at": "2026-10-20T00:05:00Z",
    },
]

REQUEST_LOG: list[dict] = []


class FakeEngine(BaseHTTPRequestHandler):
    """Bearer auth, one scope list, the engine's error bodies, and nothing else."""

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

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def do_POST(self) -> None:  # noqa: N802 - the engine has no write route either
        self._record("POST")
        self._error(405, "method_not_allowed", "the v1 surface has no write route here")

    def _record(self, method: str) -> dict:
        from urllib.parse import parse_qs, urlparse

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

    def do_GET(self) -> None:  # noqa: N802
        self._record("GET")
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if FakeEngine.scenario == "no_service_tokens":
            self._error(
                503,
                "service_auth_not_configured",
                "SERVICE_TOKENS is not set; refusing /v1",
            )
            return
        bearer = (
            (self.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        )
        if not bearer:
            self._error(401, "invalid_token", "bearer token required")
            return
        if bearer != GOOD_TOKEN:
            self._error(401, "invalid_token", "invalid token")
            return
        scope = query.get("account_scope", "")
        if not scope:
            self._error(
                400,
                "invalid_account_scope",
                "account_scope must be a slug of 1-64 chars",
            )
            return
        if scope not in SCOPES:
            # The engine answers 403 without naming the scopes a token holds.
            self._error(
                403,
                "wrong_scope",
                "this token may not serve the requested account_scope",
            )
            return

        if parsed.path == "/v1/leads":
            self._send(
                200, {"account_scope": scope, "leads": LEAD_ROWS, "next_cursor": None}
            )
        elif parsed.path == "/v1/review-queue":
            self._send(
                200,
                {
                    "account_scope": scope,
                    "queue": [
                        {
                            "external_id": "sc-synthetic-0002",
                            "reason": "models could not decide; queued for a human label",
                        }
                    ],
                },
            )
        elif parsed.path == "/v1/metrics":
            self._send(
                200,
                {"account_scope": scope, "totals": {"qualified": 1, "needs_human": 1}},
            )
        elif re.fullmatch(r"/v1/leads/[^/]+", parsed.path):
            external_id = parsed.path.rsplit("/", 1)[1]
            lead = LEADS.get(external_id)
            if lead is None:
                self._error(404, "lead_not_found", f"no lead {external_id} in {scope}")
                return
            self._send(
                200,
                {"external_id": external_id, "account_scope": scope, "snapshots": []},
            )
        elif re.fullmatch(r"/v1/leads/[^/]+/verdict", parsed.path):
            external_id = parsed.path.rsplit("/", 2)[1]
            verdict = LEADS.get(external_id)
            if verdict is None:
                self._error(
                    404, "lead_not_found", f"no verdict for {external_id} in {scope}"
                )
                return
            self._send(200, verdict)
        else:
            self._error(404, "not_found", f"no route {parsed.path}")


# ---- driving the CLI -------------------------------------------------------------


def run_cli(argv: list[str], env: dict[str, str], tool_dir: Path = TOOL_DIR) -> dict:
    environ = {
        **os.environ,
        "QWALO_ENGINE_URL": "",
        "QWALO_ACCOUNT_SCOPE": "",
        "QWALO_ENGINE_TOKEN": "",
    }
    environ.update(env)
    proc = subprocess.run(
        ["uvx", "--from", str(tool_dir), "engine", *argv],
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


def cli_env(
    base_url: str, scope: str = "ere", token: str = GOOD_TOKEN
) -> dict[str, str]:
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
    out = run_cli(["leads", "--limit", "5"], cli_env(base))
    ids = [row["external_id"] for row in out["json"].get("leads", [])]
    return {
        "ran": out,
        "ok": out["exit"] == 0 and ids == list(LEADS),
        "detail": "two synthetic leads, newest first",
    }


@case
def lead_is_read(base: str) -> dict:
    out = run_cli(["lead", "sc-synthetic-0001"], cli_env(base))
    return {
        "ran": out,
        "ok": out["exit"] == 0
        and out["json"].get("external_id") == "sc-synthetic-0001",
    }


@case
def verdict_is_read(base: str) -> dict:
    out = run_cli(["verdict", "sc-synthetic-0001"], cli_env(base))
    body = out["json"]
    ok = (
        out["exit"] == 0
        and body.get("verdict") == "qualified"
        and body.get("probability_qualified") == 0.93
        and "thresholds" in body
    )
    return {
        "ran": out,
        "ok": ok,
        "detail": "verdict, probability, and thresholds come back intact",
    }


@case
def queue_is_read(base: str) -> dict:
    out = run_cli(["queue", "--limit", "5"], cli_env(base))
    queue = out["json"].get("queue", [])
    return {
        "ran": out,
        "ok": out["exit"] == 0 and len(queue) == 1 and "reason" in queue[0],
    }


@case
def metrics_are_read(base: str) -> dict:
    out = run_cli(["metrics"], cli_env(base))
    return {"ran": out, "ok": out["exit"] == 0 and "totals" in out["json"]}


@case
def bearer_token_is_sent(base: str) -> dict:
    before = len(REQUEST_LOG)
    out = run_cli(["metrics"], cli_env(base))
    seen = REQUEST_LOG[before:]
    return {
        "ran": out,
        "ok": all(r["authorization"] == f"Bearer {GOOD_TOKEN}" for r in seen)
        and out["exit"] == 0,
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
        "ok": out["exit"] != 0
        and out["json"].get("error") == "service_auth_not_configured",
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
    out = run_cli(
        ["metrics"], {"QWALO_ACCOUNT_SCOPE": "ere", "QWALO_ENGINE_TOKEN": GOOD_TOKEN}
    )
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
    out = run_cli(
        ["metrics"], {"QWALO_ENGINE_URL": base, "QWALO_ENGINE_TOKEN": GOOD_TOKEN}
    )
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
    verbs = sorted(
        re.findall(r"\{([a-z,]+)\}", out["json"].get("_stdout", ""))[0].split(",")
    )
    source = (TOOL_DIR / "client.py").read_text()
    write_calls = re.findall(
        r"method\s*=\s*[\"'](?!GET)|https?\.?(post|put|patch|delete)\(", source
    )
    return {
        "ok": verbs == ["lead", "leads", "metrics", "queue", "verdict"]
        and not write_calls,
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
        mutated = source.replace(
            "if self.token and self.token != PLACEHOLDER:", "if self.token:"
        )
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
    """The values template must survive the real chart, not just a string grep."""
    if not shutil.which("helm"):
        return {
            "ok": True,
            "skipped": True,
            "detail": "helm is not installed here; run this suite where it is",
        }
    repo = OVERLAY_DIR.parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        values = Path(tmp) / "values.ere.yaml"
        text = (DEPLOY_DIR / "values.client.yaml.tmpl").read_text()
        for token, value in {
            "<<CLIENT>>": "ere",
            "<<NAMESPACE>>": "centaur-ere",
            "<<HOST>>": "ere.qwalo.raava.dev",
            "<<ENGINE_URL>>": "https://qwalo.raava.dev",
            "<<ENGINE_SCOPE>>": "ere",
            "<<REGISTRY>>": "",
            "<<TAG>>": "latest",
            "<<TOOL_REF>>": "main",
        }.items():
            text = text.replace(token, value)
        values.write_text(text)
        proc = subprocess.run(
            [
                "helm",
                "template",
                "centaur-ere",
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
    prompt_line = (
        (OVERLAY_DIR / "services/sandbox/SYSTEM_PROMPT.md").read_text().splitlines()[0]
    )

    def scoped(line_text: str, value: str) -> bool:
        lines = [line for line in rendered.splitlines() if line_text in line]
        return bool(lines) and all(value in line for line in lines)

    want = {
        "tool dirs carry the overlay tools": "overlays/qwalo/tools" in rendered,
        "engine url reaches the sandbox with its host": scoped(
            "QWALO_ENGINE_URL", "qwalo.raava.dev"
        ),
        "engine scope reaches the sandbox": scoped("QWALO_ACCOUNT_SCOPE", '\\"ere\\"'),
        "one host per client": 'host: "ere.qwalo.raava.dev"' in rendered,
        "system prompt is rendered in": prompt_line in rendered,
        "no literal slack secret": "SLACK_BOT_TOKEN"
        not in rendered.split("secretKeyRef")[0],
    }
    return {
        "ok": all(want.values()),
        "assertions": want,
        "resources": rendered.count("\nkind:"),
    }


@case
def only_the_engine_tool_ships(base: str) -> dict:
    shipped = sorted(
        d.name
        for d in (OVERLAY_DIR / "tools").iterdir()
        if d.is_dir() and d.name != "personas"
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
    subdirs = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("subdir:")
    ]
    telegram = REPO_DIR / "tools" / "comms" / "telegram" / "client.py"
    base_can_send = telegram.exists() and "send_message" in telegram.read_text()
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
    return {
        "argv": argv,
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
        and manifest["settings"]["event_subscriptions"]["bot_events"]
        == ["app_mention"],
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
        if re.match(
            r"^(export\s+)?[A-Z_]*(TOKEN|SECRET|PASSWORD)[A-Z_]*=", line.strip()
        )
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
        and "QWALO_ENGINE_TOKEN"
        in re.search(r"VAULT_ITEMS=\(([^)]*)\)", script).group(1),
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
    unpinned = run_script(
        [script, "--check", "ere"], {"QWALO_IMAGE_TAG": "", "QWALO_TOOL_REF": ""}
    )
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
    offenders = []
    for path in sorted(p for p in OVERLAY_DIR.rglob("*") if p.is_file()):
        text = path.read_text(errors="ignore")
        if "https://" + "://" in text or "http:" + "//" + "://" in text:
            offenders.append(str(path.relative_to(OVERLAY_DIR)))
    return {"ok": not offenders, "offenders": offenders}


@case
def deploy_script_gates_the_apply(base: str) -> dict:
    script = (DEPLOY_DIR / "deploy-client.sh").read_text()
    # The order that keeps a half-deploy from happening: check refs, then secrets,
    # then namespace, then helm.
    order = [
        script.index("cannot read op://"),
        script.index("bootstrap-k8s-secrets.sh"),
        script.index("helm upgrade --install"),
    ]
    return {
        "ok": order == sorted(order)
        and "set -euo pipefail" in script
        and "mktemp -d" in script,
        "detail": "secret refs are proven before apply; the rendered values file is temporary",
    }


@case
def deploy_script_refuses_a_token_that_sees_other_vaults(base: str) -> dict:
    """One 1Password service account per client vault, enforced before any read."""
    script = (DEPLOY_DIR / "deploy-client.sh").read_text()
    gate = {
        "lists_vaults": "op vault list" in script,
        "names_client_vaults": "prod-qwalo-${CLIENT}" in script,
        "refuses_before_secrets": script.index("op vault list")
        < script.index("op read"),
    }
    return {"ok": all(gate.values()), "checks": gate}


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
        "fake_engine": {
            "bearer": GOOD_TOKEN,
            "account_scopes": SCOPES,
            "fixtures": "synthetic only",
        },
        "passed": passed,
        "skipped": skipped,
        "total": len(results),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n"
    )
    print(
        f"\n{passed}/{len(results)} passed — artifact at {REPORT.relative_to(REPO_DIR)}"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
