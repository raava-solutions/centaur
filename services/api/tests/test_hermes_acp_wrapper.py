"""Deterministic tests for the Hermes ACP bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[2] / "sandbox" / "hermes-acp-wrapper.py"


FAKE_ACP = r'''
import json
import os
import sys
import time


def read_frame(stream):
    body = stream.readline()
    if not body:
        return None
    return json.loads(body)


def write_frame(payload, fragmented=False):
    body = json.dumps(payload, separators=(",", ":")).encode()
    frame = body + b"\n"
    if fragmented:
        for chunk in (frame[:3], frame[3:11], frame[11:]):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


def log(item):
    path = os.environ.get("FAKE_ACP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item) + "\n")


def read_response():
    while True:
        payload = read_frame(sys.stdin.buffer)
        if payload is None:
            return None
        if payload.get("method") == "session/cancel":
            log({"kind": "cancel", "payload": payload})
            continue
        return payload


session_id = "acp-session-1"
while True:
    request = read_frame(sys.stdin.buffer)
    if request is None:
        break
    log({"kind": "request", "payload": request})
    method = request.get("method")
    if method == "initialize":
        write_frame({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}})
    elif method == "session/new":
        write_frame({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": session_id}})
    elif method == "session/prompt":
        text = request["params"]["prompt"][0]["text"]
        if os.environ.get("FAKE_ACP_ERROR") == "1":
            write_frame({
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32000, "message": "fake prompt failure"},
            }, fragmented=True)
            continue
        write_frame({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "thinking"},
                },
            },
        }, fragmented=True)
        if text == "first":
            write_frame({
                "jsonrpc": "2.0",
                "id": 900,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "options": [{"kind": "reject_once", "name": "Reject", "optionId": "reject"}],
                    "toolCall": {"toolCallId": "tool-1"},
                },
            })
            log({"kind": "permission_response", "payload": read_response()})
            write_frame({
                "jsonrpc": "2.0",
                "id": 901,
                "method": "client/unsupported",
                "params": {},
            })
            log({"kind": "unsupported_response", "payload": read_response()})
            write_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tool-1",
                        "title": "blocked tool",
                        "status": "failed",
                    },
                },
            })
            answer = "first answer"
        else:
            answer = "second answer"
        time.sleep(0.1)
        write_frame({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": answer},
                },
            },
        })
        write_frame({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"stopReason": "end_turn"},
        }, fragmented=True)
    elif method == "session/cancel":
        log({"kind": "cancel", "payload": request})
'''


def _run_wrapper(
    tmp_path: Path,
    prompts: list[str],
    *,
    error: bool = False,
    interrupt_after_prompt: bool = False,
) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "fake_acp.py"
    fake.write_text(FAKE_ACP, encoding="utf-8")
    log_path = tmp_path / "fake.log"
    env = {
        **os.environ,
        "CENTAUR_HERMES_ACP_COMMAND": f"{sys.executable} {fake}",
        "FAKE_ACP_LOG": str(log_path),
    }
    if error:
        env["FAKE_ACP_ERROR"] = "1"
    input_data = "".join(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
        )
        + "\n"
        for prompt in prompts
    )
    if interrupt_after_prompt:
        input_data += json.dumps({"type": "interrupt"}) + "\n"
    return subprocess.run(
        [sys.executable, str(WRAPPER)],
        cwd=tmp_path,
        input=input_data,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=10,
    )


def _events(result: subprocess.CompletedProcess[str]) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_wrapper_bridges_fragmented_acp_frames_and_reuses_session(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, ["first", "second"])

    assert result.returncode == 0, result.stderr
    events = _events(result)
    assert events[0] == {"type": "system", "subtype": "init", "session_id": "acp-session-1"}
    assert [event["type"] for event in events].count("result") == 2
    assert [event["result"] for event in events if event["type"] == "result"] == [
        "first answer",
        "second answer",
    ]
    assert any(event["type"] == "reasoning" for event in events)
    assert any(event["type"] == "command_execution" for event in events)

    records = [json.loads(line) for line in (tmp_path / "fake.log").read_text().splitlines()]
    requests = [record["payload"] for record in records if record["kind"] == "request"]
    assert [request["method"] for request in requests] == [
        "initialize",
        "session/new",
        "session/prompt",
        "session/prompt",
    ]
    assert requests[2]["params"]["sessionId"] == requests[3]["params"]["sessionId"]
    permission = next(record for record in records if record["kind"] == "permission_response")
    assert permission["payload"]["result"] == {"outcome": {"outcome": "cancelled"}}
    unsupported = next(record for record in records if record["kind"] == "unsupported_response")
    assert unsupported["payload"]["error"]["code"] == -32601


def test_wrapper_marks_acp_text_chunks_as_deltas(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, ["first"])

    assert result.returncode == 0, result.stderr
    text_events = [
        event
        for event in _events(result)
        if event["type"] in {"assistant", "reasoning"}
    ]
    assert text_events
    assert all(event["delta"] is True for event in text_events)


def test_wrapper_surfaces_acp_error_without_fabricating_result(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, ["failure"], error=True)

    assert result.returncode == 1
    events = _events(result)
    assert events[0]["type"] == "system"
    assert events[-1] == {"type": "error", "error": "Hermes ACP session/prompt failed: fake prompt failure"}
    assert not any(event["type"] == "result" for event in events)


def test_wrapper_forwards_interrupt_as_acp_cancel(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, ["first"], interrupt_after_prompt=True)

    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in (tmp_path / "fake.log").read_text().splitlines()]
    cancel = next(record for record in records if record["kind"] == "cancel")
    assert cancel["payload"]["method"] == "session/cancel"
    assert cancel["payload"]["params"]["sessionId"] == "acp-session-1"


def test_sandbox_image_pins_hermes_source_and_acp_dependency() -> None:
    dockerfile = WRAPPER.parent / "Dockerfile"
    contents = dockerfile.read_text(encoding="utf-8")

    assert 'ARG HERMES_REPO=https://github.com/zaycruz/hermes-agent' in contents
    assert 'ARG HERMES_COMMIT=cf1c9975c4dfeef1a9ffddaab12d83440d09c79c' in contents
    assert 'ARG HERMES_ARCHIVE_SHA256=eaa8a076f8b506365a43fd29dec40a8b7b8278576e330a13a771fc397061b85c' in contents
    assert '"${HERMES_REPO}/archive/${HERMES_COMMIT}.tar.gz"' in contents
    assert 'sha256sum -c -' in contents
    assert '-e "/opt/hermes-agent[acp]"' in contents
    assert '"agent-client-protocol==${HERMES_ACP_VERSION}"' in contents
    assert "COPY --link --chmod=755 services/sandbox/hermes-acp-wrapper.py" in contents


def test_wrapper_repeated_turns_do_not_lose_parent_eof(tmp_path: Path) -> None:
    for _ in range(5):
        result = _run_wrapper(tmp_path, ["first", "second"])
        assert result.returncode == 0, result.stderr
