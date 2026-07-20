"""Client request-shape tests against an httpx MockTransport — no network."""

from __future__ import annotations

import json

import httpx
import pytest

from xtrace_cli.client import XTraceAPIError, XTraceClient

BASE = "https://api.example.test"


def _client(handler):
    transport = httpx.MockTransport(handler)
    return XTraceClient(BASE, "xtk_secret", client=httpx.Client(transport=transport))


def _capture():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["query"] = dict(request.url.params)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"ok": True})

    return handler, seen


def test_auth_header_is_token_scheme():
    handler, seen = _capture()
    with _client(handler) as c:
        c.get_memory("m1")
    assert seen["auth"] == "Token xtk_secret"


def test_ingest_payload_and_path():
    handler, seen = _capture()
    with _client(handler) as c:
        c.ingest(
            messages=[{"role": "user", "content": "hi"}],
            user_id="alice",
            conv_id="conv-1",
            namespace="repo-x",
            agentic=True,
            group_ids=["g1"],
        )
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/memories/"
    body = seen["body"]
    assert body["user_id"] == "alice"
    assert body["conv_id"] == "conv-1"
    assert body["agentic"] is True
    assert body["namespace"] == "repo-x"
    assert body["group_ids"] == ["g1"]
    # Unset optionals are omitted, not sent as null.
    assert "agent_id" not in body and "app_id" not in body


def test_search_scope_and_mode():
    handler, seen = _capture()
    with _client(handler) as c:
        c.search("food prefs", user_id="alice", mode="retrieve")
    assert seen["path"] == "/v1/memories/search"
    assert seen["body"] == {"query": "food prefs", "mode": "retrieve", "user_id": "alice"}


def test_trigger_builds_action_and_include():
    handler, seen = _capture()
    with _client(handler) as c:
        c.trigger(
            tool="Edit",
            args={"file_path": "tool_loop.py"},
            task="fix abort",
            namespace="repo-x",
            include=["lesson"],
            user_id="alice",
        )
    body = seen["body"]
    assert seen["path"] == "/v1/memories/trigger"
    assert body["action"] == {"tool": "Edit", "args": {"file_path": "tool_loop.py"}}
    assert body["include"] == ["lesson"]
    assert body["task"] == "fix abort"
    assert body["namespace"] == "repo-x"


def test_list_uses_query_params():
    handler, seen = _capture()
    with _client(handler) as c:
        c.list_memories(user_id="alice", type="fact", limit=5)
    assert seen["method"] == "GET"
    assert seen["path"] == "/v1/memories/"
    assert seen["query"] == {"user_id": "alice", "type": "fact", "limit": "5"}


def test_job_get_revisions_delete_paths():
    handler, seen = _capture()
    with _client(handler) as c:
        c.get_job("job-9")
        assert seen["path"] == "/v1/memories/jobs/job-9"
        c.get_revisions("m1")
        assert seen["path"] == "/v1/memories/m1/revisions"
        c.delete_memory("m1")
        assert seen["method"] == "DELETE" and seen["path"] == "/v1/memories/m1"


def test_error_envelope_is_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": {"code": "bad", "message": "no scope"}})

    with pytest.raises(XTraceAPIError) as ei:
        with _client(handler) as c:
            c.search("q", user_id="alice")
    assert ei.value.status_code == 422
    assert "no scope" in str(ei.value)
