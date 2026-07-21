"""Tests for the Hermes plugin bridge, register(), and install-plugin."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from xtrace_cli.cli import app
from xtrace_cli.integrations.hermes import hermes_plugin as plug
from xtrace_cli.integrations.hermes.hermes_plugin import _bridge as br

runner = CliRunner()


# ── bridge: command construction ─────────────────────────────────────────
def test_build_search_cmd():
    assert br.build_search_cmd("food") == ["xmem", "search", "food", "--mode", "compose", "--json"]


def test_build_recall_cmd():
    cmd = br.build_recall_cmd(tool="Edit", file="loop.py", task="fix", entities=["AbortError"])
    assert "--tool" in cmd and cmd[cmd.index("--tool") + 1] == "Edit"
    assert "--arg" in cmd and "file_path=loop.py" in cmd
    assert "--entity" in cmd and "AbortError" in cmd
    assert cmd[cmd.index("--task") + 1] == "fix"


# ── bridge: formatting ───────────────────────────────────────────────────
def test_format_prefers_context():
    assert br.format_payload({"context": "  do X  ", "data": [{"memory": "y"}]}) == "do X"


def test_format_falls_back_to_rows():
    assert br.format_payload({"context": None, "data": [{"memory": "a"}, {"text": "b"}]}) == "- a\n- b"
    assert br.format_payload({"data": []}) == "(no relevant memory)"


# ── bridge: run() with an injected runner ────────────────────────────────
def test_run_formats_json():
    out = br.run(["xmem", "search", "x"], runner=lambda cmd, t: (0, json.dumps({"context": "hi"}), ""))
    assert out == "hi"


def test_run_reports_error():
    out = br.run(["xmem", "x"], runner=lambda cmd, t: (1, "", "boom"))
    assert out == "xmem error: boom"


def test_run_passes_through_non_json():
    out = br.run(["xmem", "x"], runner=lambda cmd, t: (0, "plain text", ""))
    assert out == "plain text"


def test_run_never_raises():
    def boom(cmd, t):
        raise RuntimeError("nope")
    assert br.run(["xmem"], runner=boom).startswith("xmem call failed")


# ── plugin register() ────────────────────────────────────────────────────
class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, *, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


def test_register_adds_both_tools(monkeypatch):
    ctx = FakeCtx()
    plug.register(ctx)
    assert set(ctx.tools) == {"xmem_search", "xmem_recall"}
    assert ctx.tools["xmem_search"]["schema"]["parameters"]["required"] == ["query"]

    # handlers guard on missing input without shelling out
    assert "Provide a 'query'" in ctx.tools["xmem_search"]["handler"]({})
    assert "Provide" in ctx.tools["xmem_recall"]["handler"]({})


def test_recall_handler_invokes_bridge(monkeypatch):
    seen = {}

    def fake_run(cmd):
        seen["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(plug, "run", fake_run)
    out = plug._recall_handler({"tool": "Edit", "file": "x.py"})
    assert out == "ok" and "--tool" in seen["cmd"]


# ── install-plugin ───────────────────────────────────────────────────────
def test_install_plugin_copies_files(tmp_path):
    plugins, skills = tmp_path / "plugins", tmp_path / "skills"
    r = runner.invoke(app, ["hermes", "install-plugin",
                            "--dest", str(plugins), "--skills-dir", str(skills)])
    assert r.exit_code == 0, r.stdout
    for f in ("__init__.py", "plugin.yaml", "_bridge.py"):
        assert (plugins / "xmem" / f).is_file()
    assert (skills / "xmem" / "SKILL.md").is_file()


def test_install_plugin_needs_force_when_exists(tmp_path):
    plugins, skills = tmp_path / "plugins", tmp_path / "skills"
    args = ["hermes", "install-plugin", "--dest", str(plugins), "--skills-dir", str(skills)]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args).exit_code == 1          # exists, no --force
    assert runner.invoke(app, args + ["--force"]).exit_code == 0
