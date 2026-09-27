"""Tests for the RBE client. No network: _call is replaced with a recorder."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("rbe_client", Path(__file__).with_name("client.py"))
rbe_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rbe_client)


class RecordingRbeClient(rbe_client.RbeClient):
    def __init__(self, response=None):
        super().__init__(api_token="test-token")
        self.calls = []
        self.response = response if response is not None else {"results": []}

    def _call(self, tool_name, arguments):
        self.calls.append({"tool": tool_name, "arguments": arguments})
        return self.response


def test_query_minimal_args():
    c = RecordingRbeClient()
    c.query("who leads platform?")
    assert c.calls == [{"tool": "query", "arguments": {"q": "who leads platform?", "limit": 10}}]


def test_query_full_args_and_limit_clamp():
    c = RecordingRbeClient()
    c.query("infra", type="runbook", limit=500, rerank=True)
    assert c.calls[0]["tool"] == "query"
    assert c.calls[0]["arguments"] == {"q": "infra", "limit": 50, "type": "runbook", "rerank": True}


def test_search_omits_optional_args():
    c = RecordingRbeClient()
    c.search("proxmox", limit=3)
    assert c.calls[0]["arguments"] == {"q": "proxmox", "limit": 3}


def test_get_page_uses_get_tool():
    c = RecordingRbeClient()
    c.get_page("systems/pve-03")
    assert c.calls[0] == {"tool": "get", "arguments": {"slug": "systems/pve-03"}}


def test_list_pages_filters():
    c = RecordingRbeClient()
    c.list_pages(type="decision", tag="infra", limit=5)
    assert c.calls[0] == {"tool": "list", "arguments": {"limit": 5, "type": "decision", "tag": "infra"}}


def test_facts_backlinks_timeline():
    c = RecordingRbeClient()
    c.facts("zay")
    c.backlinks("systems/pve-03")
    c.timeline("zay")
    assert [call["tool"] for call in c.calls] == ["facts", "backlinks", "timeline"]
    assert c.calls[0]["arguments"] == {"entity_slug": "zay"}
    assert c.calls[1]["arguments"] == {"slug": "systems/pve-03"}
    assert c.calls[2]["arguments"] == {"entity_slug": "zay"}


def test_missing_token_raises():
    c = rbe_client.RbeClient(api_token="")
    c._api_token = ""
    import centaur_sdk

    orig = rbe_client.secret
    rbe_client.secret = lambda key, default=None: ""
    try:
        with pytest.raises(RuntimeError, match="RBE_API_TOKEN"):
            c.query("x")
    finally:
        rbe_client.secret = orig


def test_unwrap_jsonrpc_content_envelope():
    c = rbe_client.RbeClient(api_token="t")
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": '{"results": [{"slug": "a"}]}'}]},
    }
    assert c._unwrap(envelope) == {"results": [{"slug": "a"}]}


def test_unwrap_plain_text_content():
    c = rbe_client.RbeClient(api_token="t")
    envelope = {"result": {"content": [{"type": "text", "text": "not json"}]}}
    assert c._unwrap(envelope) == {"text": "not json"}


def test_unwrap_raises_on_jsonrpc_error():
    c = rbe_client.RbeClient(api_token="t")
    with pytest.raises(RuntimeError, match="RBE error"):
        c._unwrap({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such tool"}})


def test_decode_sse_takes_last_data_frame():
    class FakeResp:
        headers = {"content-type": "text/event-stream"}
        text = 'data: {"jsonrpc":"2.0","id":1,"result":{"a":1}}\n\ndata: [DONE]\n'

    c = rbe_client.RbeClient(api_token="t")
    assert c._decode(FakeResp()) == {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}


def test_factory_returns_client():
    assert isinstance(rbe_client._client(), rbe_client.RbeClient)
