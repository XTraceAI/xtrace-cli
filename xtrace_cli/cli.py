"""The ``xmem`` command.

M1 wires configuration (``xmem config set/show/path``) and the shared client
factory. The 8 memory subcommands land in M2 on top of ``XTraceClient``.
"""

from __future__ import annotations

import json
import sys

import typer

from . import __version__
from .client import XTraceClient
from .config import Config, ConfigError

app = typer.Typer(
    name="xmem",
    help="Scriptable CLI for XTrace hosted memory (/v1/memories).",
    no_args_is_help=True,
    add_completion=False,
)

config_app = typer.Typer(help="Manage the CLI configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


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


def get_client(cfg: Config | None = None) -> XTraceClient:
    """Build a client from resolved config. Shared by the M2 subcommands."""
    cfg = cfg or Config.load()
    try:
        key = cfg.require_key()
    except ConfigError as e:
        _err(str(e))
        raise typer.Exit(code=2)
    return XTraceClient(cfg.base_url, key)


# ── config ───────────────────────────────────────────────────────────────
@config_app.command("set")
def config_set(
    api_key: str = typer.Option(None, "--api-key", help="Org API key (xtk_…)."),
    base_url: str = typer.Option(None, "--base-url", help="Memory API base URL."),
    user_id: str = typer.Option(None, "--user-id", help="Default user scope."),
    agent_id: str = typer.Option(None, "--agent-id", help="Default agent scope."),
    app_id: str = typer.Option(None, "--app-id", help="Default app scope."),
    namespace: str = typer.Option(None, "--namespace", help="Default namespace (working context)."),
) -> None:
    """Write config values to ~/.config/xtrace-cli/config.yaml.

    Only the flags you pass are updated; existing values are preserved. The
    file is written 0600 because it holds the API key.
    """
    # Start from the persisted file only (not env), so we don't bake transient
    # env overrides into the file.
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
def config_show(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show the resolved config (file + env overlay). API key is masked."""
    cfg = Config.load()
    view = cfg.redacted()
    if as_json:
        typer.echo(json.dumps(view, indent=2))
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
    """Load only the on-disk config, ignoring env vars — so ``config set`` edits
    the file without persisting transient ``XTRACE_*`` overrides."""
    import os

    from .config import _ENV

    saved = {k: os.environ.pop(v, None) for k, v in _ENV.items()}
    try:
        return Config.load()
    finally:
        for env_name, val in ((_ENV[k], v) for k, v in saved.items()):
            if val is not None:
                os.environ[env_name] = val


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
