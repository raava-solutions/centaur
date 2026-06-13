#!/usr/bin/env python3
"""Centaur sandbox tool bridge.

This CLI gives Codex a stable bridge surface for Centaur API-backed tools
without copying provider credentials into the sandbox environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _read_file(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _api_url() -> str:
    return os.environ.get("CENTAUR_API_URL", "http://api:8000").rstrip("/")


def _api_key() -> str:
    return _read_file("/home/agent/.api_key") or os.environ.get("CENTAUR_API_KEY", "")


def _trace_id() -> str:
    return _read_file("/home/agent/.trace_id") or os.environ.get("CENTAUR_TRACE_ID", "")


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"{_api_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    trace_id = _trace_id()
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    thread_key = os.environ.get("CENTAUR_THREAD_KEY", "")
    if thread_key:
        headers["X-Centaur-Thread-Key"] = thread_key
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"Centaur API error {exc.code} for {path}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Centaur API transport error for {path}: {exc}") from exc

    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")


def _json_arg(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("JSON body must be an object")
    return payload


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge to Centaur API tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("tools", help="List available tools")

    discover = subparsers.add_parser("discover", help="Describe one tool")
    discover.add_argument("tool")

    call = subparsers.add_parser("call", help="Call a tool method")
    call.add_argument("tool")
    call.add_argument("method")
    call.add_argument("body", nargs="?", help="JSON object body")

    args = parser.parse_args(argv)
    if args.command == "tools":
        _print(_request("GET", "/tools"))
        return 0
    if args.command == "discover":
        tool = urllib.parse.quote(args.tool, safe="")
        _print(_request("GET", f"/tools/{tool}"))
        return 0
    if args.command == "call":
        tool = urllib.parse.quote(args.tool, safe="")
        method = urllib.parse.quote(args.method, safe="")
        _print(_request("POST", f"/tools/{tool}/{method}", _json_arg(args.body)))
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc

