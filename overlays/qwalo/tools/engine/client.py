"""Qwalo engine client — the agent's only view of lead data.

GET-only by construction. One helper issues HTTP in this file and it issues GET
requests, so there is no write path for a prompt-injected agent to find
(`FAILURE_MODES.md` F2, F3). The engine itself has no send route, and this
overlay ships no SmarterContact capability at all.

Auth and tenancy come from the deployment environment, never from the caller:

- ``QWALO_ENGINE_URL``    engine base URL for this release, e.g. https://qwalo.raava.dev
- ``QWALO_ACCOUNT_SCOPE`` the one ``account_scope`` this release may read
- ``QWALO_ENGINE_TOKEN``  bearer token, for local runs and tests only. In a
                          sandbox this variable does not exist: the secret is
                          iron-proxy inject-mode, which seeds no placeholder env
                          var, and the real bearer is added in flight

Two things limit what the engine will answer, and both live on the engine
(``src/v1/auth.ts``): the slot's ``account_scopes`` list, and its ``caps``. A
Qwalo release holds a slot with ``caps: ["read"]``, so the bearer cannot write
even if a request is forged by hand: a write answers 403
``insufficient_capability`` and a foreign scope answers 403 ``wrong_scope``, and
the tool reports either code verbatim. What the token is *not* is
method-scoped in transit: iron-proxy stamps it on any request to the engine
host, which is why ``RELEASE.md`` keeps the read-only slot as a go-live gate
(``FAILURE_MODES.md`` F2).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOKEN_ENV = "QWALO_ENGINE_TOKEN"
# Defence in depth, not the sandbox path. ``placeholder_env()`` in
# centaur-iron-proxy seeds *replace-mode* secrets as an env var whose value is
# its own name; this secret is inject-mode, so a sandbox has no such variable at
# all. If an operator sets one by hand, or the tool is ever moved to replace
# mode, a value equal to its own name is not a credential and is not sent.
PLACEHOLDER = TOKEN_ENV
USER_AGENT = "centaur-qwalo-overlay/1.0"
# Where a human goes to read the words this answer summarises. Overlay-side config,
# not an engine field: ``console_url`` is stamped by this client, and the engine never
# sends it. Unset means no link is added, which is better than a guessed host.
CONSOLE_URL_ENV = "QWALO_CONSOLE_URL"
MAX_ERROR_BYTES = 8_192


class EngineClient:
    """Read-only client for one client's engine ``account_scope``."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        account_scope: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        # Two pieces of configuration and one deliberate absence. The engine host and
        # the tenant boundary are rendered per client by the deploy, so they belong in
        # the environment. The bearer does not: iron-proxy injects it in flight (F4),
        # so TOKEN_ENV resolves to "" inside a sandbox, and _require_config turns that
        # into a named error rather than an unauthenticated call. Nothing here reads a
        # credential from the environment; nothing here could.
        self.base_url = (base_url or os.environ.get("QWALO_ENGINE_URL", "")).rstrip("/")  # noqa: TID251
        self.token = os.environ.get(TOKEN_ENV, "") if token is None else token  # noqa: TID251
        self.account_scope = account_scope or os.environ.get("QWALO_ACCOUNT_SCOPE", "")  # noqa: TID251
        self.timeout = timeout

    # ---- public read surface (the whole tool) ---------------------------------

    def list_leads(self, limit: int = 20, status: str = "", cursor: str = "") -> dict[str, Any]:
        """One keyset page of this client's leads, newest first.

        Answers ``{account_scope, leads, next_cursor}``; ``next_cursor`` is the
        opaque position to hand back as ``cursor``, and null on the last page.
        ``status`` is one of pending, final, needs_human, error; a name the
        engine does not know comes back as 400 ``invalid_query`` naming it. The
        upstream surface also filters by ``verdict`` and ``tier``; this tool does
        not expose them yet. There is no verb for ``GET /v1/settings`` or
        ``GET /v1/documents/{kind}`` either; both are follow-ups on the PR.
        """
        params: dict[str, Any] = {"limit": _clamped(limit, 1, 100)}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._get("/v1/leads", params)

    def lead(self, external_id: str) -> dict[str, Any]:
        """One lead: the view, the thresholds in force, and every model reading.

        Carries ``console_url`` when ``QWALO_CONSOLE_URL`` is configured, so a
        reviewer can read the seller's own words where they live instead of receiving
        a copy of them (``FAILURE_MODES.md`` F13).
        """
        return _with_console_link(self._get(f"/v1/leads/{_quote(external_id)}"))

    def verdict(self, external_id: str) -> dict[str, Any]:
        """The current verdict: P(qualified), confidence, stage, thresholds, label.

        A human label wins the answer at full confidence, so this may disagree
        with a stale model reading elsewhere on the surface, by design.
        """
        return _with_console_link(self._get(f"/v1/leads/{_quote(external_id)}/verdict"))

    def review_queue(self, limit: int = 20) -> dict[str, Any]:
        """Cases the models could not decide, waiting for a human label.

        The same row shape as ``leads``, filtered to status ``needs_human``.
        """
        return self._get("/v1/review-queue", {"limit": _clamped(limit, 1, 100)})

    def metrics(self, window: str = "7d") -> dict[str, Any]:
        """Counts, review rate, and model-vs-human agreement over ``window``.

        ``window`` is 7d, 30d, 90d, or all; the answer names the window it
        covered with its ``since`` and ``until``, so a widened window cannot be
        read as a stale one. Any other name is the engine's 400, verbatim.
        """
        return self._get("/v1/metrics", {"window": window})

    # ---- the one HTTP helper --------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for problem in (self._url_problem(), self._scope_problem()):
            if problem:
                return problem
        query = {"account_scope": self.account_scope}
        if params:
            query.update(params)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        headers = {"accept": "application/json", "user-agent": USER_AGENT}
        # In production iron-proxy injects the bearer (pyproject inject-mode
        # secret) and this environment variable is absent. A real env token is
        # only present for local runs and E2E.
        if self.token and self.token != PLACEHOLDER:
            headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            return _http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return {
                "error": "engine_unreachable",
                "message": f"cannot reach the engine at {self.base_url}: {reason}",
            }
        try:
            parsed = json.loads(body)
        except ValueError:
            return {
                "error": "engine_bad_response",
                "message": f"engine reply for {path} is not JSON",
            }
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    def _url_problem(self) -> dict[str, Any] | None:
        if self.base_url:
            return None
        return {
            "error": "engine_url_missing",
            "message": "this release has no QWALO_ENGINE_URL; ask the operator",
        }

    def _scope_problem(self) -> dict[str, Any] | None:
        if self.account_scope:
            return None
        # Fail closed rather than send a scope-less request: the engine would
        # answer 400 and a model could read that as "no leads".
        return {
            "error": "account_scope_missing",
            "message": "this release has no QWALO_ACCOUNT_SCOPE; ask the operator",
        }


def _clamped(value: int, low: int, high: int) -> int:
    """Bound a page size, total on any input.

    The CLI types ``--limit`` as an int, but a tool call built from a JSON body can
    hand this a string. Every other failure path returns a named ``engine_*`` code and
    exits 1; a traceback here would be the one place the tool breaks that promise, so
    anything that is not a number becomes the smallest page instead.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(number, high))


def console_url(external_id: str) -> str:
    """The console page for one lead, or an empty string when unconfigured.

    The console is hash-routed: ``#/review?id=<lead id>`` is what its router resolves
    (``public/app.js`` in qwalo-console, where ``/lead/<id>`` redirects to it), and for
    a home-scope lead the id a ``/v1`` reader holds as ``external_id`` *is* that id.

    A message-level anchor does not exist yet, so the link lands on the lead and the
    answer names which message it summarised. Tracked as console bead ``qc-sv2`` (the
    anchor) and engine bead ``qe-4eu`` (``/v1`` carries no message id to anchor on).
    """
    base = os.environ.get(CONSOLE_URL_ENV, "").strip().rstrip("/")  # noqa: TID251
    return f"{base}/#/review?id={_quote(external_id)}" if base else ""


def _with_console_link(body: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``console_url`` onto a successful lead answer; touch nothing else.

    Keyed off the ``external_id`` the engine echoed, not the caller's argument, so the
    link cannot out-run the record the answer is actually about. An error body has no
    ``external_id``, and gets no link.
    """
    external_id = body.get("external_id") if isinstance(body, dict) else None
    if isinstance(external_id, str) and external_id:
        link = console_url(external_id)
        if link:
            body["console_url"] = link
    return body


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value).strip(), safe="")


def _http_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    """Carry the engine's own error code through, so the agent names the reason.

    A 401 here means the release's engine token is missing, expired, or was
    never injected; a 403 means it does not hold this ``account_scope``. Both
    are operator faults, not empty data (`FAILURE_MODES.md` F4).
    """
    detail: Any = None
    try:
        detail = json.loads(exc.read(MAX_ERROR_BYTES))
    except Exception:
        detail = None
    error = detail.get("error") if isinstance(detail, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return {
            "error": error["code"],
            "message": str(error.get("message", ""))[:500],
            "http_status": exc.code,
        }
    return {
        "error": "engine_http_error",
        "message": f"engine answered {exc.code}",
        "http_status": exc.code,
    }


def _client() -> EngineClient:
    return EngineClient()
