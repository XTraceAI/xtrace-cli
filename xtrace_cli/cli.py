"""The ``xmem`` command — a scriptable client for XTrace hosted memory.

Config lives in a ``config`` subcommand group; the 8 memory operations are
top-level commands on top of :class:`~xtrace_cli.client.XTraceClient`. Every
read command takes ``--json`` for machine output.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from . import __version__, output
from .client import XTraceAPIError, XTraceClient
from .config import Config, ConfigError

app = typer.Typer(
    name="xmem",
    help="Scriptable CLI for XTrace hosted memory (/v1/memories).",
    no_args_is_help=True,
    add_completion=False,
)

config_app = typer.Typer(help="Manage the CLI configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

# Ingest-job statuses that mean "stop polling".
_TERMINAL_JOB_STATUS = {"completed", "succeeded", "success", "done", "failed", "error", "cancelled"}


def _err(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"xmem {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    _version: bool = typer.Option(
        False, "--version", "-V", callback=_version_cb, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """XTrace memory CLI."""


# ── shared plumbing ──────────────────────────────────────────────────────
def get_client(cfg: Config | None = None) -> XTraceClient:
    """Build a client from resolved config."""
    cfg = cfg or Config.load()
    try:
        key = cfg.require_key()
    except ConfigError as e:
        _err(str(e))
        raise typer.Exit(code=2)
    return XTraceClient(cfg.base_url, key)


def _call(fn: Callable[[], Any]) -> Any:
    """Run a client call, turning API/transport errors into clean CLI exits."""
    try:
        return fn()
    except XTraceAPIError as e:
        _err(f"API error {e.status_code}: {e}")
        raise typer.Exit(code=1)
    except Exception as e:  # noqa: BLE001 — connection/timeout etc.
        _err(f"Request failed: {type(e).__name__}: {e}")
        raise typer.Exit(code=1)


def _pick(flag: Optional[str], default: Optional[str]) -> Optional[str]:
    """Explicit flag wins over the configured default."""
    return flag if flag is not None else default


def _require_scope(user_id, agent_id, app_id, group_ids) -> None:
    if not any((user_id, agent_id, app_id, group_ids)):
        _err(
            "A scope is required — pass --user-id (or --agent-id/--app-id/--group-id), "
            "or set a default with `xmem config set --user-id …`."
        )
        raise typer.Exit(code=2)


def _parse_messages(
    message: list[str], messages_file: Optional[Path], read_stdin: bool
) -> list[dict]:
    """Assemble the ingest message list from --message, a JSON file, and/or stdin."""
    msgs: list[dict] = []
    if messages_file:
        msgs.extend(_coerce_messages(json.loads(messages_file.read_text())))
    if read_stdin or (not message and not messages_file and not sys.stdin.isatty()):
        raw = sys.stdin.read().strip()
        if raw:
            msgs.extend(_coerce_messages(json.loads(raw)))
    for m in message:
        role, sep, content = m.partition(":")
        if not sep:
            raise typer.BadParameter(f"--message must be 'role:content', got {m!r}")
        msgs.append({"role": role.strip(), "content": content.strip()})
    if not msgs:
        raise typer.BadParameter("no messages — use --message, --messages-file, or pipe JSON on stdin")
    return msgs


def _coerce_messages(data: Any) -> list[dict]:
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        data = data["messages"]
    if not isinstance(data, list):
        raise typer.BadParameter("message JSON must be a list of {role, content} objects")
    out = []
    for m in data:
        if not (isinstance(m, dict) and "role" in m and "content" in m):
            raise typer.BadParameter(f"bad message object: {m!r}")
        out.append(m)
    return out


def _parse_args_kv(pairs: list[str]) -> dict:
    """Parse ``--arg k=v`` items; values are JSON-decoded when possible."""
    out: dict[str, Any] = {}
    for p in pairs:
        key, sep, val = p.partition("=")
        if not sep:
            raise typer.BadParameter(f"--arg must be 'key=value', got {p!r}")
        try:
            out[key] = json.loads(val)
        except ValueError:
            out[key] = val
    return out


# ── config group ─────────────────────────────────────────────────────────
@config_app.command("set")
def config_set(
    api_key: str = typer.Option(None, "--api-key", help="Org API key (xtk_…)."),
    base_url: str = typer.Option(None, "--base-url", help="Memory API base URL."),
    user_id: str = typer.Option(None, "--user-id", help="Default user scope."),
    agent_id: str = typer.Option(None, "--agent-id", help="Default agent scope."),
    app_id: str = typer.Option(None, "--app-id", help="Default app scope."),
    namespace: str = typer.Option(None, "--namespace", help="Default namespace (working context)."),
) -> None:
    """Write config values to ~/.config/xtrace-cli/config.yaml (0600)."""
    cfg = _load_file_only()
    updates = {
        "api_key": api_key, "base_url": base_url, "user_id": user_id,
        "agent_id": agent_id, "app_id": app_id, "namespace": namespace,
    }
    changed = [k for k, v in updates.items() if v is not None]
    if not changed:
        _err("Nothing to set — pass at least one option (e.g. --api-key).")
        raise typer.Exit(code=2)
    for k, v in updates.items():
        if v is not None:
            setattr(cfg, k, v)
    path = cfg.save()
    typer.secho(f"Wrote {', '.join(changed)} to {path}", fg=typer.colors.GREEN)


@config_app.command("show")
def config_show(as_json: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Show the resolved config (file + env overlay). API key is masked."""
    view = Config.load().redacted()
    if as_json:
        output.emit_json(view)
        return
    width = max(len(k) for k in view)
    for k, v in view.items():
        typer.echo(f"{k.rjust(width)} : {v if v is not None else '-'}")


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file path."""
    from .config import config_path

    typer.echo(str(config_path()))


def _load_file_only() -> Config:
    """Load only the on-disk config, ignoring env, so ``config set`` doesn't bake
    in transient ``XTRACE_*`` overrides."""
    import os

    from .config import _ENV

    saved = {k: os.environ.pop(v, None) for k, v in _ENV.items()}
    try:
        return Config.load()
    finally:
        for env_name, val in ((_ENV[k], v) for k, v in saved.items()):
            if val is not None:
                os.environ[env_name] = val


# ── 1. ingest ────────────────────────────────────────────────────────────
@app.command()
def ingest(
    conv_id: str = typer.Option(..., "--conv-id", "-c", help="Conversation id (required)."),
    user_id: str = typer.Option(None, "--user-id", "-u", help="User id (or config default)."),
    message: list[str] = typer.Option([], "--message", "-m", help="A turn as 'role:content' (repeatable)."),
    messages_file: Optional[Path] = typer.Option(None, "--messages-file", help="JSON file: list of {role,content,date?}."),
    read_stdin: bool = typer.Option(False, "--stdin", help="Read the message JSON from stdin."),
    agentic: bool = typer.Option(False, "--agentic", help="Use the agentic (task-aware) ingest path."),
    extract_artifacts: bool = typer.Option(False, "--extract-artifacts", help="Also store extracted artifacts."),
    namespace: str = typer.Option(None, "--namespace", help="Working context (or config default)."),
    agent_id: str = typer.Option(None, "--agent-id"),
    app_id: str = typer.Option(None, "--app-id"),
    group_id: list[str] = typer.Option([], "--group-id", help="Group id (repeatable)."),
    timestamp_format: str = typer.Option(None, "--timestamp-format", help="strptime format for message dates (batch path)."),
    wait: bool = typer.Option(False, "--wait", help="If async, poll the job until it finishes."),
    poll_interval: float = typer.Option(2.0, "--poll-interval", help="Seconds between polls with --wait."),
    timeout: float = typer.Option(300.0, "--timeout", help="Max seconds to wait with --wait."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Ingest a message list — POST /v1/memories/."""
    cfg = Config.load()
    uid = _pick(user_id, cfg.user_id)
    if not uid:
        _err("--user-id is required (or set a default with `xmem config set --user-id …`).")
        raise typer.Exit(code=2)
    msgs = _parse_messages(message, messages_file, read_stdin)
    client = get_client(cfg)
    res = _call(lambda: client.ingest(
        messages=msgs, user_id=uid, conv_id=conv_id,
        agent_id=_pick(agent_id, cfg.agent_id), app_id=_pick(app_id, cfg.app_id),
        namespace=_pick(namespace, cfg.namespace), group_ids=group_id or None,
        agentic=agentic, extract_artifacts=extract_artifacts,
        timestamp_format=timestamp_format,
    ))
    job_id = res.get("job_id") or res.get("id") if isinstance(res, dict) else None
    if wait and job_id and (res or {}).get("status") not in _TERMINAL_JOB_STATUS:
        res = _poll_job(client, job_id, poll_interval, timeout)
    if as_json:
        output.emit_json(res)
    else:
        output.render_ingest(res)


def _poll_job(client: XTraceClient, job_id: str, interval: float, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        res = _call(lambda: client.get_job(job_id))
        status = str(res.get("status", "")).lower() if isinstance(res, dict) else ""
        if status in _TERMINAL_JOB_STATUS:
            return res
        if time.monotonic() >= deadline:
            _err(f"Timed out after {timeout}s waiting for job {job_id} (last status: {status or '?'}).")
            raise typer.Exit(code=1)
        time.sleep(interval)


# ── 2. job ───────────────────────────────────────────────────────────────
@app.command()
def job(
    job_id: str = typer.Argument(..., help="Ingest job id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Poll an async ingest job — GET /v1/memories/jobs/{id}."""
    res = _call(lambda: get_client().get_job(job_id))
    if as_json:
        output.emit_json(res)
    else:
        status = res.get("status") if isinstance(res, dict) else res
        typer.echo(f"job {job_id}: {status}")
        output.render_ingest(res)


# ── 3. search ────────────────────────────────────────────────────────────
@app.command()
def search(
    query: str = typer.Argument(..., help="Natural-language query."),
    user_id: str = typer.Option(None, "--user-id", "-u"),
    agent_id: str = typer.Option(None, "--agent-id"),
    app_id: str = typer.Option(None, "--app-id"),
    group_id: list[str] = typer.Option([], "--group-id"),
    mode: str = typer.Option("compose", "--mode", help="compose | retrieve."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Semantic memory search — POST /v1/memories/search."""
    cfg = Config.load()
    uid, aid, apid = _pick(user_id, cfg.user_id), _pick(agent_id, cfg.agent_id), _pick(app_id, cfg.app_id)
    _require_scope(uid, aid, apid, group_id)
    res = _call(lambda: get_client(cfg).search(
        query, user_id=uid, agent_id=aid, app_id=apid,
        group_ids=group_id or None, mode=mode,  # type: ignore[arg-type]
    ))
    output.emit_json(res) if as_json else output.render_rows(res, title="search")


# ── 4. recall (trigger) ──────────────────────────────────────────────────
@app.command()
def recall(
    tool: str = typer.Option(None, "--tool", help="Tool/MCP name about to run."),
    arg: list[str] = typer.Option([], "--arg", help="Intended tool arg as key=value (repeatable)."),
    tool_output: str = typer.Option(None, "--output", help="Recent tool output (post-call firing)."),
    entity: list[str] = typer.Option([], "--entity", help="Pre-extracted symbol to fire on (repeatable)."),
    task: str = typer.Option(None, "--task", help="Current goal, one line (used by compose)."),
    namespace: str = typer.Option(None, "--namespace", help="Working context (or config default)."),
    include: list[str] = typer.Option([], "--include", help="lesson and/or procedure (default both)."),
    mode: str = typer.Option("compose", "--mode", help="compose | retrieve."),
    user_id: str = typer.Option(None, "--user-id", "-u"),
    agent_id: str = typer.Option(None, "--agent-id"),
    app_id: str = typer.Option(None, "--app-id"),
    group_id: list[str] = typer.Option([], "--group-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Procedural-memory recall (pre-tool-call hook) — POST /v1/memories/trigger."""
    cfg = Config.load()
    uid, aid, apid = _pick(user_id, cfg.user_id), _pick(agent_id, cfg.agent_id), _pick(app_id, cfg.app_id)
    _require_scope(uid, aid, apid, group_id)
    if not tool and not entity:
        _err("Pass --tool (with optional --arg) or at least one --entity to fire recall.")
        raise typer.Exit(code=2)
    res = _call(lambda: get_client(cfg).trigger(
        tool=tool, args=_parse_args_kv(arg) or None, output=tool_output,
        entities=entity or None, task=task, namespace=_pick(namespace, cfg.namespace),
        include=include or None, mode=mode,  # type: ignore[arg-type]
        user_id=uid, agent_id=aid, app_id=apid, group_ids=group_id or None,
    ))
    output.emit_json(res) if as_json else output.render_rows(res, title="recall")


# ── 5. list ──────────────────────────────────────────────────────────────
@app.command(name="list")
def list_(
    user_id: str = typer.Option(None, "--user-id", "-u"),
    agent_id: str = typer.Option(None, "--agent-id"),
    app_id: str = typer.Option(None, "--app-id"),
    group_id: list[str] = typer.Option([], "--group-id"),
    type_: str = typer.Option(None, "--type", help="fact|episode|artifact|lesson|procedure."),
    limit: int = typer.Option(None, "--limit"),
    cursor: str = typer.Option(None, "--cursor"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List memories for a scope — GET /v1/memories/."""
    cfg = Config.load()
    uid, aid, apid = _pick(user_id, cfg.user_id), _pick(agent_id, cfg.agent_id), _pick(app_id, cfg.app_id)
    _require_scope(uid, aid, apid, group_id)
    res = _call(lambda: get_client(cfg).list_memories(
        user_id=uid, agent_id=aid, app_id=apid, group_ids=group_id or None,
        type=type_, limit=limit, cursor=cursor,  # type: ignore[arg-type]
    ))
    output.emit_json(res) if as_json else output.render_rows(res, title="memories")


# ── 6. get (+ revisions) ─────────────────────────────────────────────────
@app.command()
def get(
    memory_id: str = typer.Argument(...),
    revisions: bool = typer.Option(False, "--revisions", help="Show the supersede chain instead."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Get one memory, or its revision chain (GET /v1/memories/{id} or /{id}/revisions)."""
    client = get_client()
    if revisions:
        res = _call(lambda: client.get_revisions(memory_id))
        output.emit_json(res) if as_json else output.render_rows(res, title="revisions")
    else:
        res = _call(lambda: client.get_memory(memory_id))
        output.emit_json(res) if as_json else output.render_memory(res)


# ── 7. delete ────────────────────────────────────────────────────────────
@app.command()
def delete(
    memory_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Retract a memory — DELETE /v1/memories/{id}."""
    if not yes and not typer.confirm(f"Delete memory {memory_id}?"):
        raise typer.Abort()
    res = _call(lambda: get_client().delete_memory(memory_id))
    if as_json:
        output.emit_json(res)
    else:
        typer.secho(f"deleted {memory_id}", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
