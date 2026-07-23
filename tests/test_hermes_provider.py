"""Tests for the XTrace Hermes memory provider (core, provider class, installer)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from xtrace_cli.cli import app
from xtrace_cli.integrations.hermes import hermes_provider as prov
from xtrace_cli.integrations.hermes.hermes_provider import _provider_core as core_mod

runner = CliRunner()


class FakeRunner:
    """Records every call; returns queued (code, stdout, stderr) responses."""

    def __init__(self, *responses):
        self.calls: list[tuple[list[str], float, str | None]] = []
        self._responses = list(responses)

    def __call__(self, cmd, timeout, stdin_text):
        self.calls.append((list(cmd), timeout, stdin_text))
        if self._responses:
            return self._responses.pop(0)
        return 0, json.dumps({"ok": True}), ""


def _core(*responses) -> tuple[core_mod.ProviderCore, FakeRunner]:
    fr = FakeRunner(*responses)
    return core_mod.ProviderCore(runner=fr), fr


TURNS = [
    {"role": "user", "content": "book a flight"},
    {"role": "assistant", "content": "on it"},
]


# ── command builders ─────────────────────────────────────────────────────
def test_build_ingest_cmd():
    cmd = core_mod.build_ingest_cmd("sess-1", namespace="demo")
    assert cmd[:2] == ["xmem", "ingest"]
    assert "--stdin" in cmd and "--agentic" in cmd and "--json" in cmd
    assert cmd[cmd.index("--conv-id") + 1] == "sess-1"
    assert cmd[cmd.index("--namespace") + 1] == "demo"
    assert "--namespace" not in core_mod.build_ingest_cmd("sess-1")


# ── message mapping ──────────────────────────────────────────────────────
def test_map_messages_basic_and_system_skip():
    msgs = [{"role": "system", "content": "sys"}] + TURNS
    mapped = core_mod.map_messages(msgs)
    assert [m["role"] for m in mapped] == ["user", "assistant"]
    assert core_mod.map_messages(msgs, include_system=True)[0]["role"] == "system"


def test_map_messages_tool_rows():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "shell", "arguments": '{"cmd": "ls"}'}}
        ]},
        {"role": "tool", "name": "shell", "content": "file.txt"},
    ]
    mapped = core_mod.map_messages(msgs)
    assert mapped[0]["content"].startswith("[tool_call] shell(")
    assert mapped[1]["content"] == "[tool:shell] file.txt"


def test_map_messages_parts_and_junk():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]},
        {"role": "user", "content": ""},          # dropped
        {"role": "user"},                          # dropped
        "not-a-dict",                              # dropped
    ]
    mapped = core_mod.map_messages(msgs)
    assert mapped == [{"role": "user", "content": "hi\n[image_url]"}]


# ── ProviderCore.ingest ──────────────────────────────────────────────────
def test_ingest_posts_mapped_json_on_stdin():
    core, fr = _core()
    ok, detail = core.ingest(TURNS, "sess-1")
    assert ok and detail == "ingested 2 turns"
    cmd, _timeout, stdin = fr.calls[0]
    assert cmd[cmd.index("--conv-id") + 1] == "sess-1"
    assert json.loads(stdin) == TURNS


def test_ingest_dedupes_unchanged_transcript():
    core, fr = _core()
    assert core.ingest(TURNS, "s")[0]
    ok, detail = core.ingest(TURNS, "s")
    assert ok and detail == "unchanged since last ingest"
    assert len(fr.calls) == 1
    # A longer transcript re-ingests.
    assert core.ingest(TURNS + [{"role": "user", "content": "more"}], "s")[0]
    assert len(fr.calls) == 2


def test_ingest_failure_does_not_commit_fingerprint():
    core, fr = _core((1, "", "boom"))
    ok, detail = core.ingest(TURNS, "s")
    assert not ok and "boom" in detail
    ok, detail = core.ingest(TURNS, "s")  # retry after failure must NOT dedupe
    assert ok and detail == "ingested 2 turns"
    assert len(fr.calls) == 2


def test_ingest_skips_empty_and_missing_conv():
    core, fr = _core()
    assert core.ingest([{"role": "user", "content": ""}], "s") == (True, "nothing to ingest")
    assert core.ingest(TURNS, "")[0] is False
    assert fr.calls == []


def test_invalidate_forces_reingest():
    core, fr = _core()
    core.ingest(TURNS, "s")
    core.invalidate("s")
    assert core.ingest(TURNS, "s")[1] == "ingested 2 turns"
    assert len(fr.calls) == 2


def test_core_never_raises():
    def boom(cmd, t, s):
        raise RuntimeError("nope")
    core = core_mod.ProviderCore(runner=boom)
    ok, detail = core.ingest(TURNS, "s")
    assert not ok and "xmem call failed" in detail


# ── binary resolution (GUI-launched Hermes has a bare PATH) ──────────────
def test_resolve_xmem_falls_back_to_known_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod.shutil, "which", lambda _: None)
    fake = tmp_path / "xmem"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(core_mod, "_fallback_candidates", lambda: [fake])
    assert core_mod.resolve_xmem() == str(fake)
    monkeypatch.setattr(core_mod, "_fallback_candidates", lambda: [tmp_path / "missing"])
    assert core_mod.resolve_xmem() is None


def test_run_substitutes_resolved_binary(tmp_path, monkeypatch):
    fake = tmp_path / "xmem"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(core_mod, "resolve_xmem", lambda: str(fake))
    seen = {}

    def spy(cmd, timeout, stdin_text):
        seen["cmd0"] = cmd[0]
        return 0, "{}", ""

    monkeypatch.setattr(core_mod, "_default_runner", spy)
    core = core_mod.ProviderCore()  # no injected runner → resolution path
    ok, _ = core.search("q")
    assert ok and seen["cmd0"] == str(fake)


def test_is_available_uses_resolver(monkeypatch):
    p = prov.XTraceMemoryProvider(config={})
    monkeypatch.setattr(prov.core_mod, "resolve_xmem", lambda: "/x/xmem")
    assert p.is_available() is True
    monkeypatch.setattr(prov.core_mod, "resolve_xmem", lambda: None)
    assert p.is_available() is False


# ── provider: prefetch & tools ───────────────────────────────────────────
def _provider(*responses, config=None):
    core, fr = _core(*responses)
    p = prov.XTraceMemoryProvider(config=config or {}, core=core)
    p.initialize("sess-1")
    return p, fr


def test_prefetch_wraps_context():
    p, fr = _provider((0, json.dumps({"context": "user prefers aisle"}), ""))
    out = p.prefetch("what are the user's flight preferences")
    assert out == "## XTrace memory\nuser prefers aisle"
    cmd = fr.calls[0][0]
    assert cmd[cmd.index("--mode") + 1] == "retrieve"  # fast default for in-loop


def test_prefetch_skips_short_disabled_empty_and_errors():
    p, fr = _provider()
    assert p.prefetch("short") == ""                      # under min length
    assert fr.calls == []
    p2, _ = _provider(config={"prefetch": False})
    assert p2.prefetch("a long enough query indeed") == ""
    p3, _ = _provider((0, json.dumps({"data": []}), ""))
    assert p3.prefetch("a long enough query indeed") == ""
    p4, _ = _provider((1, "", "401"))
    assert p4.prefetch("a long enough query indeed") == ""


def test_tool_calls_return_json():
    p, _ = _provider((0, json.dumps({"context": "ctx"}), ""))
    res = json.loads(p.handle_tool_call("xmem_search", {"query": "q"}))
    assert res == {"result": "ctx"}
    p2, fr2 = _provider()
    json.loads(p2.handle_tool_call("xmem_recall", {"tool": "Edit", "file": "a.py"}))
    cmd = fr2.calls[0][0]
    assert cmd[:2] == ["xmem", "recall"] and "file_path=a.py" in cmd


def test_tool_errors():
    p, _ = _provider()
    assert "error" in json.loads(p.handle_tool_call("xmem_search", {}))
    assert "error" in json.loads(p.handle_tool_call("xmem_recall", {}))
    assert "error" in json.loads(p.handle_tool_call("nope", {}))


# ── provider: capture lifecycle ──────────────────────────────────────────
def _join_bg(p):
    for t in p._bg_threads:
        t.join(timeout=5.0)


def test_session_end_flushes_under_session_id():
    p, fr = _provider()
    p.sync_turn("u", "a", messages=TURNS)
    p.on_session_end(TURNS)
    cmd, _t, stdin = fr.calls[0]
    assert cmd[cmd.index("--conv-id") + 1] == "sess-1"
    assert json.loads(stdin) == TURNS


def test_shutdown_is_noop_after_session_end_flush():
    p, fr = _provider()
    p.sync_turn("u", "a", messages=TURNS)
    p.on_session_end(TURNS)
    p.shutdown()
    assert len(fr.calls) == 1  # snapshot cleared + fingerprint dedupe


def test_shutdown_flushes_unflushed_snapshot():
    p, fr = _provider()
    p.sync_turn("u", "a", messages=TURNS)
    p.shutdown()  # no session end (crash-ish path)
    assert len(fr.calls) == 1
    assert json.loads(fr.calls[0][2]) == TURNS


def test_pre_compress_flushes_in_background():
    p, fr = _provider()
    p.on_pre_compress(TURNS)
    _join_bg(p)
    assert len(fr.calls) == 1


def test_session_switch_reset_flushes_old_conversation():
    p, fr = _provider()
    p.sync_turn("u", "a", messages=TURNS)
    p.on_session_switch("sess-2", reset=True)
    _join_bg(p)
    cmd = fr.calls[0][0]
    assert cmd[cmd.index("--conv-id") + 1] == "sess-1"  # old id, captured at switch
    assert p._session_id == "sess-2"
    p.on_session_end(TURNS)  # new session's flush targets the new id
    assert fr.calls[-1][0][fr.calls[-1][0].index("--conv-id") + 1] == "sess-2"


def test_session_switch_rewound_invalidates_fingerprint():
    p, fr = _provider()
    p.on_session_end(TURNS)
    p.on_session_switch("sess-1", rewound=True)
    p.on_session_end(TURNS)  # same content — would dedupe without invalidate
    assert len(fr.calls) == 2


def test_non_primary_context_never_writes():
    core, fr = _core()
    p = prov.XTraceMemoryProvider(config={}, core=core)
    p.initialize("sess-1", agent_context="cron")
    p.sync_turn("u", "a", messages=TURNS)
    p.on_session_end(TURNS)
    p.on_pre_compress(TURNS)
    p.shutdown()
    assert fr.calls == []


def test_auto_ingest_off_disables_capture():
    p, fr = _provider(config={"auto_ingest": False})
    p.on_session_end(TURNS)
    assert fr.calls == []


# ── provider: registration & setup ───────────────────────────────────────
def test_register_captures_provider():
    class Ctx:
        provider = None

        def register_memory_provider(self, p):
            self.provider = p

    ctx = Ctx()
    prov.register(ctx)
    assert isinstance(ctx.provider, prov.XTraceMemoryProvider)
    assert ctx.provider.name == "xtrace"
    names = [s["name"] for s in ctx.provider.get_tool_schemas()]
    assert names == ["xmem_search", "xmem_recall"]


def test_save_config_shells_to_xmem_config_set():
    p, fr = _provider()
    p.save_config({"user_id": "alice", "namespace": "demo", "api_key": "SECRET"}, "/tmp/h")
    cmd = fr.calls[0][0]
    assert cmd[:3] == ["xmem", "config", "set"]
    assert cmd[cmd.index("--user-id") + 1] == "alice"
    assert "SECRET" not in cmd  # secrets go to .env, never through config set


def test_config_schema_marks_key_secret():
    p, _ = _provider()
    schema = {f["key"]: f for f in p.get_config_schema()}
    assert schema["api_key"]["secret"] is True
    assert schema["api_key"]["env_var"] == "XTRACE_API_KEY"


# ── installer ────────────────────────────────────────────────────────────
def test_install_provider(tmp_path):
    res = runner.invoke(app, ["hermes", "install-provider", "--dest", str(tmp_path)])
    assert res.exit_code == 0, res.output
    target = tmp_path / "xtrace"
    assert (target / "__init__.py").is_file()
    assert (target / "_provider_core.py").is_file()
    assert (target / "plugin.yaml").is_file()
    assert "memory:" in res.output and "provider: xtrace" in res.output


def test_install_provider_warns_about_plugin_duplication(tmp_path):
    (tmp_path / "xmem").mkdir()
    res = runner.invoke(app, ["hermes", "install-provider", "--dest", str(tmp_path)])
    assert res.exit_code == 0
    assert "disable xmem" in res.output


def test_install_provider_refuses_overwrite_without_force(tmp_path):
    assert runner.invoke(app, ["hermes", "install-provider", "--dest", str(tmp_path)]).exit_code == 0
    assert runner.invoke(app, ["hermes", "install-provider", "--dest", str(tmp_path)]).exit_code == 1
    res = runner.invoke(app, ["hermes", "install-provider", "--dest", str(tmp_path), "--force"])
    assert res.exit_code == 0
