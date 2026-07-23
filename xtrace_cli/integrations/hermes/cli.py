"""``xmem hermes`` — session-store glue for Nous Research hermes-agent.

Registered onto the root app in ``xtrace_cli.cli``. Core helpers (client
factory, scope resolution, job polling) are imported lazily inside the command
bodies to avoid an import cycle with ``xtrace_cli.cli``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer

from xtrace_cli import output
from xtrace_cli.config import Config

from . import session_ingest as si

_HERE = Path(__file__).parent
_BUNDLED_PLUGIN = _HERE / "hermes_plugin"
_BUNDLED_PROVIDER = _HERE / "hermes_provider"
_BUNDLED_SKILL = _HERE / "skill"

hermes_app = typer.Typer(help="Nous Research hermes-agent integration.", no_args_is_help=True)


def _db_path(db: Optional[Path]) -> Path:
    return db if db is not None else si.default_db_path()


@hermes_app.command("sessions")
def sessions(
    db: Optional[Path] = typer.Option(None, "--db", help="Path to state.db (default ~/.hermes/state.db)."),
    limit: int = typer.Option(20, "--limit", "-n", help="How many recent sessions to show."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List recent Hermes sessions (to find a session id to ingest)."""
    try:
        rows = si.list_sessions(_db_path(db), limit=limit)
    except si.HermesStoreError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if as_json:
        output.emit_json([r.__dict__ for r in rows])
        return
    if not rows:
        typer.secho("no sessions found", fg=typer.colors.YELLOW)
        return
    for r in rows:
        title = (r.title or "").strip().replace("\n", " ")
        tail = f"  msgs={r.message_count}" if r.message_count is not None else ""
        typer.echo(f"  {r.id}{tail}  {title[:60]}")


@hermes_app.command("ingest")
def ingest(
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Hermes session id."),
    latest: bool = typer.Option(False, "--latest", help="Ingest the most recent session."),
    db: Optional[Path] = typer.Option(None, "--db", help="Path to state.db (default ~/.hermes/state.db)."),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="User id (or config default)."),
    conv_id: Optional[str] = typer.Option(None, "--conv-id", "-c", help="Conversation id (default: the session id)."),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Working context (or config default)."),
    agentic: bool = typer.Option(True, "--agentic/--no-agentic", help="Agentic ingest path (default on for Hermes)."),
    extract_artifacts: bool = typer.Option(False, "--extract-artifacts"),
    include_system: bool = typer.Option(False, "--include-system", help="Include the system prompt turn."),
    tools: bool = typer.Option(True, "--tools/--no-tools", help="Include tool-result turns (default on)."),
    group_id: list[str] = typer.Option([], "--group-id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be ingested; don't call the API."),
    wait: bool = typer.Option(False, "--wait", help="If async, poll the job until it finishes."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Ingest a completed Hermes session into XTrace memory.

    Reads the session's turns from Hermes' local state.db and POSTs them via the
    agentic ingest path. ``conv_id`` defaults to the Hermes session id so the
    memories anchor back to the source session.
    """
    from xtrace_cli.cli import _pick, _poll_job, get_client, _TERMINAL_JOB_STATUS  # lazy: avoid cycle

    cfg = Config.load()
    path = _db_path(db)

    # Resolve the session id.
    try:
        if latest and not session:
            session = si.latest_session_id(path)
            if not session:
                typer.secho(f"no sessions in {path}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
        if not session:
            typer.secho("pass --session <id> or --latest.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        info, messages = si.read_session(
            path, session, include_system=include_system, include_tools=tools
        )
    except si.HermesStoreError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not messages:
        typer.secho(f"session {session} has no ingestable turns", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)

    uid = _pick(user_id, cfg.user_id)
    if not uid:
        typer.secho("--user-id is required (or set `xmem config set --user-id …`).",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    cid = conv_id or info.id
    ns = _pick(namespace, cfg.namespace)

    if dry_run:
        _print_dry_run(info, messages, uid, cid, ns, agentic, as_json)
        return

    client = get_client(cfg)
    try:
        res = client.ingest(
            messages=messages, user_id=uid, conv_id=cid,
            namespace=ns, group_ids=group_id or None,
            agentic=agentic, extract_artifacts=extract_artifacts,
        )
        job_id = res.get("job_id") or res.get("id") if isinstance(res, dict) else None
        if wait and job_id and (res or {}).get("status") not in _TERMINAL_JOB_STATUS:
            res = _poll_job(client, job_id, 2.0, 300.0)
    except Exception as e:  # noqa: BLE001
        typer.secho(f"ingest failed: {type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if as_json:
        output.emit_json(res)
    else:
        typer.echo(f"session {info.id} → {len(messages)} turns (agentic={agentic}, namespace={ns or '-'})")
        output.render_ingest(res)


@hermes_app.command("install-plugin")
def install_plugin(
    dest: Optional[Path] = typer.Option(None, "--dest", help="Plugins dir (default ~/.hermes/plugins)."),
    skills_dir: Optional[Path] = typer.Option(None, "--skills-dir", help="Skills dir (default ~/.hermes/skills)."),
    with_skill: bool = typer.Option(True, "--with-skill/--no-skill", help="Also install the xtrace-memory skill."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing install."),
) -> None:
    """Install the xmem Hermes plugin (xmem_search + xmem_recall tools).

    Copies the bundled plugin to ``~/.hermes/plugins/xmem/`` (auto-discovered by
    Hermes) and, unless ``--no-skill``, the xtrace-memory skill to
    ``~/.hermes/skills/xmem/``. Requires the ``xmem`` CLI on PATH and a
    configured key.
    """
    home = si.hermes_home()
    plugin_target = (dest or home / "plugins") / "xmem"
    _copy_tree(_BUNDLED_PLUGIN, plugin_target, force, label="plugin")

    if with_skill:
        skill_target = (skills_dir or home / "skills") / "xmem"
        _copy_tree(_BUNDLED_SKILL, skill_target, force, label="skill")

    typer.secho(f"Installed xmem plugin → {plugin_target}", fg=typer.colors.GREEN)
    typer.echo("Hermes auto-discovers plugins under ~/.hermes/plugins on next start.")
    typer.echo("Ensure the `xmem` CLI is on PATH and `xmem config set --api-key <xtk_…>` is done.")


@hermes_app.command("install-provider")
def install_provider(
    dest: Optional[Path] = typer.Option(None, "--dest", help="Plugins dir (default ~/.hermes/plugins)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing install."),
) -> None:
    """Install XTrace as Hermes' external memory provider (full-backend mode).

    Copies the bundled provider to ``~/.hermes/plugins/xtrace/``. Unlike the
    additive plugin, the provider prefetches context into every turn and
    ingests the session automatically at boundaries — but it occupies Hermes'
    single external memory-provider slot. Activate with ``memory.provider:
    xtrace`` in ``~/.hermes/config.yaml`` (or ``hermes memory``).
    """
    home = si.hermes_home()
    plugins_dir = dest or home / "plugins"
    _copy_tree(_BUNDLED_PROVIDER, plugins_dir / "xtrace", force, label="provider")

    typer.secho(f"Installed xtrace memory provider → {plugins_dir / 'xtrace'}", fg=typer.colors.GREEN)
    typer.echo("Activate it:  add to ~/.hermes/config.yaml:\n"
               "  memory:\n"
               "    provider: xtrace\n"
               "then restart hermes. Requires `xmem` on PATH with a configured key.")
    if (plugins_dir / "xmem").exists():
        typer.secho(
            "Note: the additive xmem tools plugin is also installed. The provider "
            "registers the same xmem_search/xmem_recall tools — disable the plugin "
            "(`hermes plugins disable xmem`) to avoid duplicates.",
            fg=typer.colors.YELLOW,
        )


def _copy_tree(src: Path, target: Path, force: bool, *, label: str) -> None:
    if not src.is_dir():
        typer.secho(f"bundled {label} not found at {src}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if target.exists():
        if not force:
            typer.secho(f"{target} exists — pass --force to overwrite.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _print_dry_run(info, messages, uid, cid, ns, agentic, as_json) -> None:
    if as_json:
        output.emit_json({
            "session": info.__dict__, "user_id": uid, "conv_id": cid,
            "namespace": ns, "agentic": agentic, "messages": messages,
        })
        return
    roles: dict[str, int] = {}
    for m in messages:
        roles[m["role"]] = roles.get(m["role"], 0) + 1
    typer.secho(f"[dry-run] session {info.id}", fg=typer.colors.CYAN)
    typer.echo(f"  turns={len(messages)}  roles={roles}")
    typer.echo(f"  user_id={uid}  conv_id={cid}  namespace={ns or '-'}  agentic={agentic}")
    for m in messages[:3]:
        typer.echo(f"    {m['role']}: {m['content'][:80]!r}")
    if len(messages) > 3:
        typer.echo(f"    … and {len(messages) - 3} more")
