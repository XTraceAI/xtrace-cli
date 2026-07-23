"""Pure core for the XTrace Hermes memory provider.

No hermes imports and no third-party deps — unit-testable and safe to copy
verbatim into ``~/.hermes/plugins/xtrace/``. The provider ``__init__`` wraps
this in Hermes' ``MemoryProvider`` ABC; everything here shells out to the
``xmem`` CLI, so the installed provider has no Python dependency on this
package (mirrors ``hermes_plugin/_bridge.py``, plus ingest + stdin support).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

XMEM_BIN = "xmem"


def _fallback_candidates() -> list[Path]:
    """Where ``xmem`` lands when PATH doesn't have it — GUI-launched Hermes
    (desktop app) gets launchd's bare PATH, not the shell profile's."""
    home = Path.home()
    return [
        home / ".local" / "bin" / XMEM_BIN,   # uv tool / pipx default
        Path("/usr/local/bin") / XMEM_BIN,
        Path("/opt/homebrew/bin") / XMEM_BIN,
    ]


def resolve_xmem() -> Optional[str]:
    """Absolute path to the ``xmem`` binary, or None. PATH first, then
    well-known install locations."""
    found = shutil.which(XMEM_BIN)
    if found:
        return found
    for c in _fallback_candidates():
        if c.is_file():
            return str(c)
    return None

SEARCH_TIMEOUT = 20.0   # prefetch/tool search — MemoryManager also enforces its own
RECALL_TIMEOUT = 20.0
INGEST_TIMEOUT = 60.0   # ingest POST; the server-side job is async anyway

# runner(cmd, timeout, stdin_text) -> (returncode, stdout, stderr)
Runner = Callable[[Sequence[str], float, Optional[str]], tuple[int, str, str]]


# ── command builders ─────────────────────────────────────────────────────
def build_search_cmd(query: str, *, mode: str = "compose") -> list[str]:
    return [XMEM_BIN, "search", query, "--mode", mode, "--json"]


def build_recall_cmd(
    *,
    tool: Optional[str] = None,
    file: Optional[str] = None,
    entities: Optional[Sequence[str]] = None,
    task: Optional[str] = None,
    namespace: Optional[str] = None,
) -> list[str]:
    cmd = [XMEM_BIN, "recall", "--mode", "compose", "--json"]
    if tool:
        cmd += ["--tool", tool]
    if file:
        cmd += ["--arg", f"file_path={file}"]
    for e in entities or []:
        cmd += ["--entity", e]
    if task:
        cmd += ["--task", task]
    if namespace:
        cmd += ["--namespace", namespace]
    return cmd


def build_ingest_cmd(conv_id: str, *, namespace: Optional[str] = None) -> list[str]:
    cmd = [XMEM_BIN, "ingest", "--stdin", "--conv-id", conv_id, "--agentic", "--json"]
    if namespace:
        cmd += ["--namespace", namespace]
    return cmd


# ── message mapping (live OpenAI-style turns → XTrace ingest format) ─────
def map_messages(
    messages: Sequence[dict],
    *,
    include_system: bool = False,
    include_tools: bool = True,
) -> list[dict[str, Any]]:
    """Hermes hands providers the OpenAI-style message list. Map it to XTrace
    ingest messages (``{role, content}``), mirroring the decisions in
    ``session_ingest.read_session``: drop empty turns, render bare assistant
    tool-call rows compactly, prefix tool results with the tool name."""
    out: list[dict[str, Any]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or ""
        if role == "system" and not include_system:
            continue
        if role == "tool" and not include_tools:
            continue
        text = _content_text(m.get("content"))
        if role == "assistant" and not text and m.get("tool_calls"):
            if not include_tools:
                # include_tools=False means NO tool activity leaves the box —
                # invocation args can carry secrets as easily as results.
                continue
            text = _render_tool_calls(m.get("tool_calls"))
        if role == "tool" and text:
            tool_name = m.get("name") or m.get("tool_name")
            if tool_name:
                text = f"[tool:{tool_name}] {text}"
        if not text:
            continue
        out.append({"role": role, "content": text})
    return out


def _content_text(raw: Any) -> str:
    """Content may be a plain string or a multimodal parts list."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for p in raw:
            if isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if t:
                    parts.append(str(t))
                elif p.get("type"):
                    parts.append(f"[{p['type']}]")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("content") or "")
    return str(raw)


def _render_tool_calls(calls: Any) -> str:
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
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


def format_payload(payload: Any) -> str:
    """A search/recall JSON response → text. Prefer compose-mode ``context``,
    fall back to a bulleted list of rows, empty string when nothing hit."""
    if isinstance(payload, dict):
        ctx = payload.get("context")
        if ctx:
            return str(ctx).strip()
        rows = payload.get("data") or []
        lines = []
        for r in rows:
            if isinstance(r, dict):
                t = r.get("memory") or r.get("text") or r.get("content")
                if t:
                    lines.append(f"- {t}")
        return "\n".join(lines)
    return str(payload or "")


def _default_runner(cmd: Sequence[str], timeout: float, stdin_text: Optional[str]) -> tuple[int, str, str]:
    p = subprocess.run(
        list(cmd), capture_output=True, text=True, timeout=timeout,
        input=stdin_text,
    )
    return p.returncode, p.stdout, p.stderr


class ProviderCore:
    """Stateful glue: search/recall/ingest via the ``xmem`` CLI, with a
    per-conversation fingerprint so boundary flushes (session end, pre-compress,
    shutdown) don't re-POST an unchanged transcript. Thread-safe; never raises.
    """

    def __init__(self, *, runner: Optional[Runner] = None, namespace: Optional[str] = None):
        self._runner = runner
        self._namespace = namespace
        self._lock = threading.Lock()
        self._last_fp: dict[str, tuple[int, int]] = {}

    # ── read side ────────────────────────────────────────────────────────
    def search(self, query: str, *, mode: str = "compose") -> tuple[bool, str]:
        return self._run(build_search_cmd(query, mode=mode), timeout=SEARCH_TIMEOUT)

    def recall(
        self,
        *,
        tool: Optional[str] = None,
        file: Optional[str] = None,
        entities: Optional[Sequence[str]] = None,
        task: Optional[str] = None,
    ) -> tuple[bool, str]:
        cmd = build_recall_cmd(
            tool=tool, file=file, entities=entities, task=task, namespace=self._namespace,
        )
        return self._run(cmd, timeout=RECALL_TIMEOUT)

    # ── write side ───────────────────────────────────────────────────────
    def ingest(
        self,
        messages: Sequence[dict],
        conv_id: str,
        *,
        include_system: bool = False,
        include_tools: bool = True,
    ) -> tuple[bool, str]:
        """Map and POST a transcript under ``conv_id``. Returns (ok, detail).

        Skips (ok=True) when the mapped transcript is empty or identical to the
        last successful ingest for this conv_id. The fingerprint is only
        committed AFTER a successful run, so a failed flush retries next time.
        """
        if not conv_id:
            return False, "no conv_id"
        mapped = map_messages(
            messages, include_system=include_system, include_tools=include_tools,
        )
        if not mapped:
            return True, "nothing to ingest"
        fp = _fingerprint(mapped)
        with self._lock:
            if self._last_fp.get(conv_id) == fp:
                return True, "unchanged since last ingest"
        payload = json.dumps(mapped)
        ok, detail = self._run(
            build_ingest_cmd(conv_id, namespace=self._namespace),
            timeout=INGEST_TIMEOUT, stdin_text=payload,
        )
        if ok:
            with self._lock:
                self._last_fp[conv_id] = fp
            detail = f"ingested {len(mapped)} turns"
        return ok, detail

    def invalidate(self, conv_id: str) -> None:
        """Forget the fingerprint (e.g. after a transcript rewind) so the next
        flush re-ingests even if lengths happen to match."""
        with self._lock:
            self._last_fp.pop(conv_id, None)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _run(
        self, cmd: Sequence[str], *, timeout: float, stdin_text: Optional[str] = None,
    ) -> tuple[bool, str]:
        if self._runner is None:
            xmem = resolve_xmem()
            if xmem is None:
                return False, (
                    "xmem CLI not found on PATH — `pip install xtrace-cli` and "
                    "`xmem config set --api-key <xtk_…>`."
                )
            # Substitute the resolved absolute path so GUI-launched Hermes
            # (bare launchd PATH) can still exec the binary.
            cmd = [xmem, *cmd[1:]]
        run_fn: Runner = self._runner or _default_runner
        try:
            code, out, err = run_fn(cmd, timeout, stdin_text)
        except Exception as e:  # noqa: BLE001 — never propagate into the agent
            return False, f"xmem call failed: {type(e).__name__}: {e}"
        if code != 0:
            return False, f"xmem error: {(err or out).strip() or 'exit ' + str(code)}"
        out = (out or "").strip()
        if not out:
            return True, ""
        try:
            return True, format_payload(json.loads(out))
        except ValueError:
            return True, out


def _fingerprint(mapped: Sequence[dict]) -> tuple[int, int]:
    """Cheap identity for a mapped transcript: (turn count, content hash)."""
    h = 0
    for m in mapped:
        h = hash((h, m.get("role"), m.get("content")))
    return len(mapped), h
