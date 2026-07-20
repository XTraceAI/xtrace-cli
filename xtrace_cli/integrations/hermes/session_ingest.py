"""Read a hermes-agent session transcript from its local SQLite store and map
it to XTrace ingest messages.

We talk to ``state.db`` directly with stdlib ``sqlite3`` (read-only) rather than
importing hermes — the CLI stays dependency-free and works by pointing at the
file. Schema (hermes ``hermes_state.py``):

- ``sessions(id TEXT PK, display_name, title, started_at REAL, ended_at REAL,
  message_count, …)``
- ``messages(id INTEGER PK AUTOINCREMENT, session_id, role, content,
  tool_call_id, tool_calls TEXT(json), tool_name, timestamp REAL, active, …)``

Load-bearing details mirrored from hermes' own read path:
- turns are ordered by ``id`` (autoincrement), NOT ``timestamp`` (which is wall
  clock and non-monotonic);
- ``active = 1`` filters soft-deleted / rewound rows;
- ``content`` may be a JSON-encoded multimodal parts list — decode to text;
- tool calls are their own rows (``role='assistant'`` with ``tool_calls`` JSON;
  ``role='tool'`` result rows with ``tool_call_id``/``tool_name``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def hermes_home() -> Path:
    """Mirror hermes' ``get_hermes_home``: ``$HERMES_HOME`` → platform default."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "hermes"
    return Path.home() / ".hermes"


def default_db_path() -> Path:
    return hermes_home() / "state.db"


@dataclass
class SessionInfo:
    id: str
    title: Optional[str]
    started_at: Optional[float]
    ended_at: Optional[float]
    message_count: Optional[int]


class HermesStoreError(Exception):
    """The Hermes state DB or a requested session couldn't be read."""


def _connect(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).is_file():
        raise HermesStoreError(
            f"Hermes state DB not found at {db_path}. Pass --db or set HERMES_HOME."
        )
    # Read-only URI open — never takes a write lock, safe against a live agent.
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def list_sessions(db_path: Path, *, limit: int = 20) -> list[SessionInfo]:
    """Most-recent sessions first (by ended_at, falling back to started_at)."""
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, title, display_name, started_at, ended_at, message_count "
            "FROM sessions ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as e:
        raise HermesStoreError(f"reading sessions: {e}") from e
    finally:
        con.close()
    return [
        SessionInfo(
            id=r["id"],
            title=r["title"] or r["display_name"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            message_count=r["message_count"],
        )
        for r in rows
    ]


def latest_session_id(db_path: Path) -> Optional[str]:
    sessions = list_sessions(db_path, limit=1)
    return sessions[0].id if sessions else None


def read_session(
    db_path: Path,
    session_id: str,
    *,
    include_system: bool = False,
    include_tools: bool = True,
) -> tuple[SessionInfo, list[dict[str, Any]]]:
    """Return (session info, XTrace ingest messages) for one session.

    Messages are ``{role, content, date}`` in insertion order. Empty turns are
    dropped; assistant tool-call rows with no text get a compact rendering of
    the call so the turn still carries signal.
    """
    con = _connect(db_path)
    try:
        srow = con.execute(
            "SELECT id, title, display_name, started_at, ended_at, message_count "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if srow is None:
            raise HermesStoreError(f"session {session_id!r} not found in {db_path}")
        rows = con.execute(
            "SELECT role, content, tool_calls, tool_name, tool_call_id, timestamp "
            "FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error as e:
        raise HermesStoreError(f"reading session {session_id}: {e}") from e
    finally:
        con.close()

    info = SessionInfo(
        id=srow["id"],
        title=srow["title"] or srow["display_name"],
        started_at=srow["started_at"],
        ended_at=srow["ended_at"],
        message_count=srow["message_count"],
    )

    messages: list[dict[str, Any]] = []
    for r in rows:
        role = r["role"]
        if role == "system" and not include_system:
            continue
        if role == "tool" and not include_tools:
            continue
        text = _decode_content(r["content"])
        if role == "assistant" and not text and r["tool_calls"]:
            text = _render_tool_calls(r["tool_calls"])
        if role == "tool" and r["tool_name"] and text:
            text = f"[tool:{r['tool_name']}] {text}"
        if not text:
            continue
        msg: dict[str, Any] = {"role": role, "content": text}
        if r["timestamp"] is not None:
            msg["date"] = _iso(r["timestamp"])
        messages.append(msg)
    return info, messages


# ── content decoding ─────────────────────────────────────────────────────
def _decode_content(raw: Any) -> str:
    """Hermes stores plain strings or a JSON multimodal parts list. Decode to
    text; a plain string that isn't JSON is returned unchanged."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return str(raw)
    s = raw.lstrip()
    if s[:1] in ("[", "{"):
        try:
            data = json.loads(s)
        except ValueError:
            return raw
        return _text_from_parts(data)
    return raw


def _text_from_parts(data: Any) -> str:
    if isinstance(data, list):
        out = []
        for p in data:
            if isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if t:
                    out.append(str(t))
                elif p.get("type"):
                    out.append(f"[{p['type']}]")
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    if isinstance(data, dict):
        return str(data.get("text") or data.get("content") or "")
    return str(data)


def _render_tool_calls(raw: Any) -> str:
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ""
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else {}
        name = fn.get("name") or c.get("name") or "tool"
        args = fn.get("arguments") if fn else c.get("arguments")
        out.append(f"[tool_call] {name}({args})" if args else f"[tool_call] {name}()")
    return "\n".join(out)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
