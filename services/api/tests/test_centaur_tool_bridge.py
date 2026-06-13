from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE = REPO_ROOT / "services" / "sandbox" / "centaur-tool-bridge.py"


class _BridgeHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def _write_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.calls.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path == "/tools":
            self._write_json({"supermemory": {"description": "memory"}})
            return
        self._write_json({"tool": self.path.rsplit("/", 1)[-1], "methods": []})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.calls.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw.decode("utf-8")),
            }
        )
        self._write_json({"tool": "supermemory", "method": "recall", "result": {"ok": True}})


def test_centaur_tool_bridge_calls_tool_with_sandbox_auth() -> None:
    _BridgeHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(BRIDGE),
                "call",
                "supermemory",
                "recall",
                '{"query":"project memory"}',
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "CENTAUR_API_URL": f"http://127.0.0.1:{server.server_port}",
                "CENTAUR_API_KEY": "sandbox-token",
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "method": "recall",
        "result": {"ok": True},
        "tool": "supermemory",
    }
    assert _BridgeHandler.calls == [
        {
            "method": "POST",
            "path": "/tools/supermemory/recall",
            "authorization": "Bearer sandbox-token",
            "body": {"query": "project memory"},
        }
    ]

