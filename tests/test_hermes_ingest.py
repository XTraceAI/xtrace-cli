"""Hermes session-store adapter tests against a synthetic state.db fixture."""

from __future__ import annotations

import json
import sqlite3

import pytest
from typer.testing import CliRunner

import xtrace_cli.cli as climod
from xtrace_cli.cli import app
from xtrace_cli.config import Config
from xtrace_cli.integrations.hermes import session_ingest as si

runner = CliRunner()


def _make_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, display_name TEXT,
            started_at REAL, ended_at REAL, message_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
            timestamp REAL, active INTEGER DEFAULT 1
        );
        """
    )
    con.execute("INSERT INTO sessions VALUES ('s1','Fix the loop',NULL,1000.0,1100.0,6)")
    con.execute("INSERT INTO sessions VALUES ('s0','Older',NULL,10.0,20.0,2)")
    rows = [
        # role, content, tool_call_id, tool_calls, tool_name, ts, active
        ("system", "You are Hermes.", None, None, None, 1000.0, 1),
        ("user", "fix the abort bug", None, None, None, 1001.0, 1),
        ("assistant", None, None, json.dumps(
            [{"function": {"name": "Edit", "arguments": "{\"file\":\"loop.py\"}"}}]), None, 1002.0, 1),
        ("tool", "patched loop.py", "c1", None, "Edit", 1003.0, 1),
        # multimodal content as a JSON parts list
        ("user", json.dumps([{"type": "text", "text": "does it pass?"}, {"type": "image"}]),
         None, None, None, 1004.0, 1),
        ("assistant", "yes, tests green", None, None, None, 1005.0, 1),
        # soft-deleted row must be skipped
        ("assistant", "SHOULD NOT APPEAR", None, None, None, 1006.0, 0),
    ]
    con.executemany(
        "INSERT INTO messages (session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,active) "
        "VALUES ('s1',?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "state.db"
    _make_db(p)
    return p


def test_read_session_mapping(db):
    info, msgs = si.read_session(db, "s1")  # defaults: no system, tools on
    assert info.id == "s1" and info.title == "Fix the loop"
    roles = [m["role"] for m in msgs]
    # system skipped, inactive skipped; order preserved by id
    assert roles == ["user", "assistant", "tool", "user", "assistant"]
    assert "SHOULD NOT APPEAR" not in json.dumps(msgs)
    # assistant tool-call row rendered from tool_calls JSON
    assert "[tool_call] Edit(" in msgs[1]["content"]
    # tool result prefixed with tool name
    assert msgs[2]["content"].startswith("[tool:Edit] patched loop.py")
    # multimodal decoded to its text part
    assert msgs[3]["content"].startswith("does it pass?")
    # date stamped as ISO-8601 UTC (fixture uses tiny epoch ts, so year is 1970)
    assert "T" in msgs[0]["date"] and msgs[0]["date"].endswith("+00:00")


def test_include_system_and_no_tools(db):
    _, with_sys = si.read_session(db, "s1", include_system=True)
    assert with_sys[0]["role"] == "system"
    _, no_tools = si.read_session(db, "s1", include_tools=False)
    assert "tool" not in [m["role"] for m in no_tools]


def test_list_and_latest(db):
    sessions = si.list_sessions(db)
    assert [s.id for s in sessions] == ["s1", "s0"]  # by ended_at desc
    assert si.latest_session_id(db) == "s1"


def test_missing_session_and_db(db, tmp_path):
    with pytest.raises(si.HermesStoreError):
        si.read_session(db, "nope")
    with pytest.raises(si.HermesStoreError):
        si.list_sessions(tmp_path / "absent.db")


# ── CLI ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def cfg_alice(monkeypatch):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(
        base_url="http://x", api_key="xtk_x", user_id="alice", namespace="repo-x")))


def test_hermes_dry_run(db, cfg_alice):
    r = runner.invoke(app, ["hermes", "ingest", "--session", "s1", "--db", str(db), "--dry-run"])
    assert r.exit_code == 0, r.stdout
    assert "turns=5" in r.stdout and "conv_id=s1" in r.stdout and "namespace=repo-x" in r.stdout


def test_hermes_ingest_calls_client(db, cfg_alice, monkeypatch):
    captured = {}

    class Fake:
        def ingest(self, **kw):
            captured.update(kw)
            return {"job_id": "j1", "status": "queued"}

    monkeypatch.setattr(climod, "get_client", lambda cfg=None: Fake())
    r = runner.invoke(app, ["hermes", "ingest", "-s", "s1", "--db", str(db), "--latest"])
    assert r.exit_code == 0, r.stdout
    assert captured["conv_id"] == "s1"          # defaults to session id
    assert captured["user_id"] == "alice"       # config default
    assert captured["agentic"] is True          # Hermes default
    assert len(captured["messages"]) == 5


def test_hermes_needs_session(db, cfg_alice):
    r = runner.invoke(app, ["hermes", "ingest", "--db", str(db)])
    assert r.exit_code == 2  # neither --session nor --latest
