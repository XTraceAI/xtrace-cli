"""CLI behavior tests — a fake client injected via get_client, no network."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import xtrace_cli.cli as climod
from xtrace_cli.cli import app
from xtrace_cli.config import Config

runner = CliRunner()


class FakeClient:
    def __init__(self):
        self.calls = []

    def search(self, query, **kw):
        self.calls.append(("search", query, kw))
        return {"object": "list", "mode": kw.get("mode"),
                "data": [{"id": "f1", "type": "fact", "memory": "likes thai", "score": 0.9}],
                "context": "User likes thai."}

    def trigger(self, **kw):
        self.calls.append(("trigger", kw))
        return {"data": [{"id": "d1", "type": "lesson", "memory": "don't touch X"}], "context": None}

    def ingest(self, **kw):
        self.calls.append(("ingest", kw))
        return {"job_id": "job-1", "status": "queued"}

    def get_job(self, job_id):
        return {"job_id": job_id, "status": "completed", "memories_created": 3}

    def list_memories(self, **kw):
        self.calls.append(("list", kw))
        return {"data": [{"id": "m1", "type": "fact", "memory": "x"}]}

    def get_memory(self, mid):
        return {"id": mid, "type": "fact", "memory": "hello"}

    def get_revisions(self, mid):
        return {"data": [{"id": mid, "type": "fact", "memory": "v2"}]}

    def delete_memory(self, mid):
        return {"id": mid, "deleted": True}


@pytest.fixture()
def fake(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(climod, "get_client", lambda cfg=None: fc)
    # Config with a default user so scope checks pass without flags.
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(
        base_url="http://x", api_key="xtk_x", user_id="alice")))
    return fc


def test_search_human_and_json(fake):
    r = runner.invoke(app, ["search", "food"])
    assert r.exit_code == 0
    assert "likes thai" in r.stdout and "composed context" in r.stdout

    r = runner.invoke(app, ["search", "food", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout)["data"][0]["id"] == "f1"


def test_search_requires_scope(monkeypatch):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(
        base_url="http://x", api_key="xtk_x")))  # no default scope
    monkeypatch.setattr(climod, "get_client", lambda cfg=None: FakeClient())
    r = runner.invoke(app, ["search", "food"])
    # Guard fires with exit code 2; the message goes to stderr (stdout stays
    # clean for --json), so assert on the contract, not the stream.
    assert r.exit_code == 2


def test_ingest_parses_messages_and_scope_default(fake):
    r = runner.invoke(app, ["ingest", "-c", "conv-1", "-m", "user:hi", "-m", "assistant:yo", "--agentic"])
    assert r.exit_code == 0, r.stdout
    call = next(c for c in fake.calls if c[0] == "ingest")
    kwargs = call[1]
    assert kwargs["user_id"] == "alice"  # from config default
    assert kwargs["conv_id"] == "conv-1"
    assert kwargs["agentic"] is True
    assert [m["role"] for m in kwargs["messages"]] == ["user", "assistant"]


def test_ingest_bad_message_format(fake):
    r = runner.invoke(app, ["ingest", "-c", "c1", "-m", "no-colon-here"])
    assert r.exit_code != 0


def test_recall_requires_tool_or_entity(fake):
    r = runner.invoke(app, ["recall"])
    assert r.exit_code == 2  # message on stderr


def test_recall_builds_action(fake):
    r = runner.invoke(app, ["recall", "--tool", "Edit", "--arg", "file_path=x.py", "--task", "fix"])
    assert r.exit_code == 0
    call = next(c for c in fake.calls if c[0] == "trigger")
    assert call[1]["tool"] == "Edit"
    assert call[1]["args"] == {"file_path": "x.py"}


def test_get_and_revisions(fake):
    assert "hello" in runner.invoke(app, ["get", "m1"]).stdout
    assert "revisions" in runner.invoke(app, ["get", "m1", "--revisions"]).stdout


def test_delete_confirm_and_yes(fake):
    # declines
    r = runner.invoke(app, ["delete", "m1"], input="n\n")
    assert r.exit_code != 0
    # --yes skips prompt
    r = runner.invoke(app, ["delete", "m1", "--yes"])
    assert r.exit_code == 0 and "deleted m1" in r.stdout


def test_list_type_filter(fake):
    r = runner.invoke(app, ["list", "--type", "fact"])
    assert r.exit_code == 0
    call = next(c for c in fake.calls if c[0] == "list")
    assert call[1]["type"] == "fact"
