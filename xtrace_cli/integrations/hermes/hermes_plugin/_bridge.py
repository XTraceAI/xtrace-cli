"""Pure glue between a hermes-agent tool call and the ``xmem`` CLI.

No hermes imports and no third-party deps — so it is unit-testable and safe to
copy verbatim into ``~/.hermes/plugins/xmem/``. The plugin ``__init__`` and any
memory-provider variant build on these functions; the handlers just shell out
to ``xmem … --json`` and format the response for the model.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable, Optional, Sequence

XMEM_BIN = "xmem"

# runner(cmd, timeout) -> (returncode, stdout, stderr)
Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


def build_search_cmd(query: str) -> list[str]:
    """`xmem search` in compose mode (prompt-ready context), JSON out."""
    return [XMEM_BIN, "search", query, "--mode", "compose", "--json"]


def build_recall_cmd(
    *,
    tool: Optional[str] = None,
    file: Optional[str] = None,
    entities: Optional[Sequence[str]] = None,
    task: Optional[str] = None,
    namespace: Optional[str] = None,
) -> list[str]:
    """`xmem recall` (procedural directives) for an in-flight tool call."""
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


def format_payload(payload: Any) -> str:
    """A search/recall JSON response → text for the model.

    Compose mode returns a prompt-ready ``context`` block; prefer it, else fall
    back to a bulleted list of the rows.
    """
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
        return "\n".join(lines) if lines else "(no relevant memory)"
    return str(payload)


def run(cmd: Sequence[str], *, runner: Optional[Runner] = None, timeout: float = 30.0) -> str:
    """Execute an ``xmem`` command and return formatted text (or a friendly
    error string — handlers should never raise into the agent loop)."""
    use_default = runner is None
    if use_default and shutil.which(XMEM_BIN) is None:
        return ("xmem CLI not found on PATH — `pip install xtrace-cli` and "
                "`xmem config set --api-key <xtk_…>`.")
    run_fn: Runner = runner or _default_runner
    try:
        code, out, err = run_fn(cmd, timeout)
    except Exception as e:  # noqa: BLE001 — never propagate into the tool loop
        return f"xmem call failed: {type(e).__name__}: {e}"
    if code != 0:
        return f"xmem error: {(err or out).strip() or 'exit ' + str(code)}"
    out = (out or "").strip()
    if not out:
        return "(no output)"
    try:
        return format_payload(json.loads(out))
    except ValueError:
        return out  # not JSON — hand back whatever it printed


def _default_runner(cmd: Sequence[str], timeout: float) -> tuple[int, str, str]:
    p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr
