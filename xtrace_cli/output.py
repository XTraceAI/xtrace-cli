"""Rendering helpers for the ``xmem`` CLI — human-readable by default, exact
JSON with ``--json``. All renderers are defensive about field names so a minor
server-side shape change degrades to "show what we got" rather than a crash.
"""

from __future__ import annotations

import json
from typing import Any

import typer


def emit_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _row_text(row: dict) -> str:
    for k in ("memory", "text", "content", "directive_text", "summary"):
        v = row.get(k)
        if v:
            return str(v)
    return ""


def _row_id(row: dict) -> str:
    for k in ("id", "memory_id", "fact_id"):
        v = row.get(k)
        if v:
            return str(v)
    return "?"


def _rows(payload: Any) -> list[dict]:
    """Pull the row list out of whatever envelope the endpoint returned."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("data", "results", "memories", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def render_rows(payload: Any, *, title: str) -> None:
    rows = _rows(payload)
    if not rows:
        typer.secho(f"{title}: no results", fg=typer.colors.YELLOW)
        _maybe_context(payload)
        return
    typer.secho(f"{title}: {len(rows)} result(s)", fg=typer.colors.GREEN)
    for r in rows:
        score = r.get("score")
        head = f"  • {_row_id(r)}"
        if r.get("type"):
            head += f"  [{r['type']}]"
        if isinstance(score, (int, float)):
            head += f"  score={score:.3f}"
        typer.echo(head)
        text = _row_text(r)
        if text:
            typer.echo(f"      {text}")
    _maybe_context(payload)


def _maybe_context(payload: Any) -> None:
    """Search/trigger in ``compose`` mode returns a prompt-ready markdown blob."""
    if isinstance(payload, dict):
        ctx = payload.get("context")
        if ctx:
            typer.secho("  ── composed context ──", fg=typer.colors.CYAN)
            for line in str(ctx).splitlines():
                typer.echo(f"  {line}")


def render_memory(payload: Any) -> None:
    if not isinstance(payload, dict):
        typer.echo(str(payload))
        return
    # A single memory may come back bare or wrapped in {data: {...}}.
    mem = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    typer.secho(f"{_row_id(mem)}", fg=typer.colors.GREEN, bold=True)
    for k in ("type", "user_id", "created_at", "namespace"):
        if mem.get(k):
            typer.echo(f"  {k}: {mem[k]}")
    text = _row_text(mem)
    if text:
        typer.echo(f"  {text}")


def render_ingest(payload: Any) -> None:
    if not isinstance(payload, dict):
        typer.echo(str(payload))
        return
    job_id = payload.get("job_id") or payload.get("id")
    status = payload.get("status")
    created = payload.get("memories_created")
    rows = _rows(payload)
    parts = []
    if job_id and status:
        parts.append(f"job={job_id} status={status}")
    if created is not None:
        parts.append(f"memories_created={created}")
    if rows:
        parts.append(f"results={len(rows)}")
    typer.secho("ingest: " + (" ".join(parts) if parts else "accepted"), fg=typer.colors.GREEN)
