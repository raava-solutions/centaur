#!/usr/bin/env python3
"""Bridge Centaur's NDJSON wire to the Hermes ACP stdio server.

Centaur owns the outer wire: one JSON object per line on stdin and stdout.
Hermes ACP uses newline-delimited JSON-RPC messages.  The bridge keeps one ACP
process and one ACP session alive for the sandbox.

The default command is installed by the sandbox image.  Tests can override it
with ``CENTAUR_HERMES_ACP_COMMAND`` to run a deterministic fake subprocess.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_ACP_COMMAND = "/opt/hermes-venv/bin/hermes-acp"
_MAX_FRAME_BYTES = 50 * 1024 * 1024
_PARENT_EOF = object()


@dataclass(frozen=True)
class _ChildMessage:
    payload: dict[str, Any] | None = None
    error: BaseException | None = None
    eof: bool = False


def _write_frame(stream: Any, payload: dict[str, Any]) -> None:
    """Write one ACP JSON-RPC message.

    ACP 0.9 uses newline-delimited JSON on stdio.  Do not use LSP-style
    ``Content-Length`` framing here.  Hermes' ``acp.Connection`` reads one
    complete JSON object from each line.
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(body + b"\n")
    stream.flush()


def _read_frame(stream: Any) -> dict[str, Any] | None:
    body = stream.readline()
    if not body:
        return None
    if len(body) > _MAX_FRAME_BYTES:
        raise ValueError("ACP message exceeds the size limit")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise TypeError("ACP message must be a JSON object")
    return payload


class _ACPProcess:
    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("failed to open Hermes ACP pipes")
        self._stdin = self.process.stdin
        self._messages: queue.Queue[_ChildMessage] = queue.Queue()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                message = _read_frame(self.process.stdout)
                if message is None:
                    self._messages.put(_ChildMessage(eof=True))
                    return
                self._messages.put(_ChildMessage(payload=message))
        except (OSError, TypeError, ValueError) as exc:
            self._messages.put(_ChildMessage(error=exc))

    def send(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            _write_frame(self._stdin, payload)

    def next_message(self, timeout: float = 0.05) -> _ChildMessage | None:
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


class _ParentInput:
    def __init__(self) -> None:
        self._messages: queue.Queue[str | object] = queue.Queue()
        self._eof = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)

    def start(self) -> None:
        self._reader.start()

    def _read_loop(self) -> None:
        for line in sys.stdin:
            self._messages.put(line)
        self._eof.set()
        self._messages.put(_PARENT_EOF)

    def next(self) -> str | object:
        try:
            return self._messages.get_nowait()
        except queue.Empty:
            if self._eof.is_set():
                return _PARENT_EOF
            return self._messages.get()

    def drain(self) -> list[str | object]:
        messages: list[str | object] = []
        while True:
            try:
                messages.append(self._messages.get_nowait())
            except queue.Empty:
                return messages


class _HermesBridge:
    def __init__(self, command: list[str], cwd: Path) -> None:
        self._child = _ACPProcess(command)
        self._cwd = cwd
        self._next_id = 1
        self._session_id: str | None = None
        self._cancel_sent = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        on_notification: Any = None,
        parent_input: _ParentInput | None = None,
        on_parent_input: Any = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._child.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self._child.next_message()
            if parent_input is not None and on_parent_input is not None:
                for parent_message in parent_input.drain():
                    on_parent_input(parent_message)
            if message is None:
                continue
            if message.error is not None:
                raise RuntimeError(f"Hermes ACP transport failed: {message.error}")
            if message.eof:
                raise RuntimeError("Hermes ACP process exited before the response")
            payload = message.payload
            if payload is None:
                continue

            if payload.get("id") == request_id and ("result" in payload or "error" in payload):
                if "error" in payload:
                    error = payload["error"]
                    text = error.get("message") if isinstance(error, dict) else str(error)
                    raise RuntimeError(f"Hermes ACP {method} failed: {text}")
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(f"Hermes ACP {method} returned an invalid result")
                return result

            if "method" in payload and "id" in payload:
                self._handle_child_request(payload)
            elif on_notification is not None and "method" in payload:
                on_notification(payload)

    def _handle_child_request(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params")
        if method == "session/request_permission":
            # Never auto-approve an ACP tool request. The parent can add an
            # approval channel later without changing the wire boundary.
            self._child.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            return

        if method == "fs/read_text_file":
            result = self._read_text_file(params)
            if isinstance(result, dict):
                self._child.send({"jsonrpc": "2.0", "id": request_id, "result": result})
            else:
                self._send_error(request_id, -32602, result)
            return

        self._send_error(request_id, -32601, f"unsupported ACP client request: {method}")

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        self._child.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _read_text_file(self, params: Any) -> dict[str, Any] | str:
        if not isinstance(params, dict) or not isinstance(params.get("path"), str):
            return "fs/read_text_file requires a path"
        requested = Path(params["path"])
        target = requested if requested.is_absolute() else self._cwd / requested
        try:
            resolved = target.resolve()
            resolved.relative_to(self._cwd.resolve())
        except (OSError, ValueError):
            return "file path is outside the sandbox workspace"
        try:
            content = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return f"cannot read file: {exc}"
        line = params.get("line")
        limit = params.get("limit")
        if isinstance(line, int) and line > 0:
            lines = content.splitlines(keepends=True)
            start = line - 1
            end = start + limit if isinstance(limit, int) and limit > 0 else None
            content = "".join(lines[start:end])
        return {"content": content}

    def initialize(self) -> str:
        self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "centaur-hermes-bridge", "version": "1"},
            },
        )
        result = self._request("session/new", {"cwd": str(self._cwd), "mcpServers": []})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Hermes ACP session/new returned no sessionId")
        self._session_id = session_id
        return session_id

    def prompt(
        self,
        content: list[dict[str, Any]],
        emit: Any,
        *,
        parent_input: _ParentInput,
        on_parent_input: Any,
    ) -> None:
        if self._session_id is None:
            raise RuntimeError("Hermes ACP session is not initialized")
        self._cancel_sent = False
        result = self._request(
            "session/prompt",
            {"sessionId": self._session_id, "prompt": content},
            on_notification=lambda payload: emit(self._notification_event(payload)),
            parent_input=parent_input,
            on_parent_input=on_parent_input,
        )
        stop_reason = result.get("stopReason")
        if stop_reason not in {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}:
            raise RuntimeError(f"Hermes ACP returned an unknown stopReason: {stop_reason}")
        emit({"type": "result", "subtype": "success" if stop_reason == "end_turn" else "error", "result": emit(None)})

    def cancel(self) -> None:
        if self._session_id is not None and not self._cancel_sent:
            self._child.send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self._session_id},
                }
            )
            self._cancel_sent = True

    def _notification_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("method") != "session/update":
            return None
        params = payload.get("params")
        if not isinstance(params, dict):
            return None
        update = params.get("update")
        if not isinstance(update, dict):
            return None
        session_id = params.get("sessionId") or self._session_id
        session_update = update.get("sessionUpdate")
        if session_update == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    return {
                        "type": "assistant",
                        "session_id": session_id,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                    }
        if session_update == "agent_thought_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    return {"type": "reasoning", "session_id": session_id, "text": text}
        if session_update in {"tool_call", "tool_call_update"}:
            tool_id = update.get("toolCallId")
            if isinstance(tool_id, str) and tool_id:
                event: dict[str, Any] = {
                    "type": "command_execution",
                    "session_id": session_id,
                    "tool_call_id": tool_id,
                    "command": update.get("title") or "Hermes tool",
                    "status": update.get("status") or "working",
                }
                raw_output = update.get("rawOutput")
                if raw_output is not None:
                    event["aggregated_output"] = (
                        raw_output if isinstance(raw_output, str) else json.dumps(raw_output, ensure_ascii=False)
                    )
                return event
        return None

    def close(self) -> None:
        self._child.close()


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    if event.get("type") == "turn.start":
        content = event.get("content")
        if isinstance(content, list):
            return [block for block in content if isinstance(block, dict)]
        text = event.get("text")
        return [{"type": "text", "text": text}] if isinstance(text, str) else []

    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            blocks.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            if source.get("type") == "base64" and isinstance(source.get("data"), str):
                blocks.append(
                    {
                        "type": "image",
                        "mimeType": source.get("media_type") or "application/octet-stream",
                        "data": source["data"],
                    }
                )
            elif isinstance(source.get("url"), str):
                blocks.append({"type": "image", "mimeType": "image/*", "data": source["url"]})
    return blocks


def _event_kind(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if event_type in {"user", "turn.start"}:
        return "prompt"
    if event_type == "interrupt":
        return "interrupt"
    return "unsupported"


def main() -> int:
    command_text = os.getenv("CENTAUR_HERMES_ACP_COMMAND", _DEFAULT_ACP_COMMAND)
    command = shlex.split(command_text)
    if not command:
        raise RuntimeError("CENTAUR_HERMES_ACP_COMMAND is empty")

    bridge = _HermesBridge(command, Path.cwd())
    parent_input = _ParentInput()
    parent_input.start()
    try:
        session_id = bridge.initialize()
        print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}), flush=True)

        pending: deque[dict[str, Any]] = deque()
        parent_closed = False

        def parse_parent_input(raw_line: str | object) -> dict[str, Any] | None:
            if raw_line is _PARENT_EOF:
                return None
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                print(json.dumps({"type": "error", "error": f"invalid Centaur input: {exc}"}), flush=True)
                return None
            if not isinstance(event, dict):
                print(json.dumps({"type": "error", "error": "Centaur input must be an object"}), flush=True)
                return None
            return event

        def defer_parent_input(raw_line: str | object) -> None:
            nonlocal parent_closed
            if raw_line is _PARENT_EOF:
                parent_closed = True
                return
            event = parse_parent_input(raw_line)
            if event is None:
                return
            if _event_kind(event) == "interrupt":
                bridge.cancel()
            else:
                pending.append(event)

        while True:
            if pending:
                event = pending.popleft()
            else:
                if parent_closed:
                    break
                raw_line = parent_input.next()
                if raw_line is _PARENT_EOF:
                    break
                event = parse_parent_input(raw_line)
                if event is None:
                    continue

            kind = _event_kind(event)
            if kind == "interrupt":
                bridge.cancel()
                continue
            if kind != "prompt":
                print(json.dumps({"type": "error", "error": f"unsupported Centaur input: {event.get('type')}"}), flush=True)
                continue

            text_parts: list[str] = []

            def emit(value: dict[str, Any] | None, *, parts: list[str] = text_parts) -> Any:
                if value is None:
                    return "".join(parts)
                if value.get("type") == "assistant":
                    message = value.get("message")
                    content = message.get("content") if isinstance(message, dict) else []
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            parts.append(block["text"])
                print(json.dumps(value, ensure_ascii=False), flush=True)
                return None

            bridge.prompt(
                _content_blocks(event),
                emit,
                parent_input=parent_input,
                on_parent_input=defer_parent_input,
            )
            if parent_closed and not pending:
                break
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
        return 1
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
