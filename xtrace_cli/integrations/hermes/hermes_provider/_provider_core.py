"""Pure core for the XTrace Hermes memory provider.

No hermes imports and no third-party deps — unit-testable and safe to copy
verbatim into ``~/.hermes/plugins/xtrace/``. The provider ``__init__`` wraps
this in Hermes' ``MemoryProvider`` ABC; everything here shells out to the
``xmem`` CLI, so the installed provider has no Python dependency on this
package (mirrors ``hermes_plugin/_bridge.py``, plus ingest + stdin support).

Capture assurance (0.2.2, from the customer field study): "accepted" is
never reported as stored. Ingest submits, then polls the job to a TERMINAL
state; only terminal success commits the dedup digest and reports "stored".
Anything else (terminal failure, still-pending at deadline, transport error)
lands in a durable receipt with the payload stashed in the on-disk outbox.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

try:
    from . import _receipts as receipts_mod
except ImportError:  # pragma: no cover - defensive for odd loaders
    import importlib.util as _ilu
    import pathlib as _pl

    _rp = _pl.Path(__file__).with_name("_receipts.py")
    _rspec = _ilu.spec_from_file_location("_xtrace_receipts", _rp)
    receipts_mod = _ilu.module_from_spec(_rspec)
    _rspec.loader.exec_module(receipts_mod)  # type: ignore[union-attr]

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


def build_job_cmd(job_id: str) -> list[str]:
    return [XMEM_BIN, "job", job_id, "--json"]


class ProviderCore:
    """Stateful glue: search/recall/ingest via the ``xmem`` CLI, with a
    per-conversation stored-digest so boundary flushes (session end,
    pre-compress, shutdown) don't re-POST an unchanged transcript.
    Thread-safe; never raises.

    ``ingest`` returns ``(state, detail)`` with state one of:
    ``stored`` (proven terminal success — the ONLY state that commits the
    dedup digest), ``pending`` (accepted, unproven at deadline), ``failed``,
    ``skipped`` (empty or unchanged-since-last-STORED).
    """

    def __init__(
        self,
        *,
        runner: Optional[Runner] = None,
        namespace: Optional[str] = None,
        receipts: Optional["receipts_mod.ReceiptStore"] = None,
        poll_interval: float = 2.0,
        poll_deadline: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._runner = runner
        self._namespace = namespace
        self._receipts = receipts
        self._poll_interval = poll_interval
        self._poll_deadline = poll_deadline
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_digest: dict[str, str] = {}
        if receipts is not None:
            # Cross-restart dedup: seed from the durable ledger.
            self._last_digest.update(receipts.last_stored_digests())

    def attach_receipts(self, receipts: "receipts_mod.ReceiptStore") -> None:
        with self._lock:
            self._receipts = receipts
            self._last_digest.update(receipts.last_stored_digests())

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
    ) -> tuple[str, str]:
        """Map, POST, and poll to a terminal state. Returns (state, detail)."""
        if not conv_id:
            return "failed", "no conv_id"
        mapped = map_messages(
            messages, include_system=include_system, include_tools=include_tools,
        )
        if not mapped:
            return "skipped", "nothing to ingest"
        digest = receipts_mod.payload_digest(mapped)
        with self._lock:
            if self._last_digest.get(conv_id) == digest:
                return "skipped", "unchanged since last stored ingest"
        return self._submit(mapped, conv_id, digest)

    def _submit(self, mapped: list[dict], conv_id: str, digest: str) -> tuple[str, str]:
        turns = len(mapped)
        ok, payload, err = self._run_json(
            build_ingest_cmd(conv_id, namespace=self._namespace),
            timeout=INGEST_TIMEOUT, stdin_text=json.dumps(mapped),
        )
        if not ok:
            return self._settle("failed", conv_id, digest, mapped, turns, None, err)
        state, job_id, detail = receipts_mod.classify_response(payload)
        if state == "pending" and job_id:
            state, detail = self._poll_to_terminal(job_id)
        return self._settle(state, conv_id, digest, mapped, turns, job_id, detail)

    def _poll_to_terminal(self, job_id: str) -> tuple[str, str]:
        deadline = time.monotonic() + self._poll_deadline
        while time.monotonic() < deadline:
            self._sleep(self._poll_interval)
            ok, payload, err = self._run_json(build_job_cmd(job_id), timeout=SEARCH_TIMEOUT)
            if not ok:
                # Transient poll error — keep trying until the deadline.
                continue
            state, _jid, detail = receipts_mod.classify_response(payload)
            if state != "pending":
                return state, detail
        return "pending", f"job {job_id} not terminal after {self._poll_deadline:.0f}s"

    def _settle(
        self,
        state: str,
        conv_id: str,
        digest: str,
        mapped: list[dict],
        turns: int,
        job_id: Optional[str],
        detail: str,
    ) -> tuple[str, str]:
        """Commit digests, receipts, and outbox according to the final state.
        The digest is committed ONLY on proven storage."""
        if state == "stored":
            with self._lock:
                self._last_digest[conv_id] = digest
            detail = f"stored {turns} turns" + (f" (job {job_id})" if job_id else "")
            if self._receipts:
                self._receipts.record(conv_id=conv_id, digest=digest, status="stored",
                                      turns=turns, job_id=job_id, detail=detail)
                self._receipts.clear_payload(conv_id, digest)
        else:
            detail = detail or f"{state} with no detail"
            if self._receipts:
                self._receipts.record(conv_id=conv_id, digest=digest, status=state,
                                      turns=turns, job_id=job_id, detail=detail)
                self._receipts.stash_payload(conv_id, digest, mapped)
        return state, detail

    # ── reconciliation (next-start recovery of unproven work) ────────────
    def reconcile(self, *, max_resubmits: int = 5) -> dict[str, int]:
        """Resolve unproven submissions from the durable ledger: poll pending
        jobs to terminal state; resubmit failed payloads from the outbox
        (bounded). Returns counters for logging."""
        counts = {"recovered": 0, "still_pending": 0, "failed": 0, "resubmitted": 0}
        if not self._receipts:
            return counts
        pending_jobs = {
            (r.get("conv_id"), r.get("digest")): r
            for r in self._receipts.unresolved() if r.get("status") == "pending" and r.get("job_id")
        }
        for (conv_id, digest), rec in pending_jobs.items():
            ok, payload, _err = self._run_json(build_job_cmd(rec["job_id"]), timeout=SEARCH_TIMEOUT)
            if not ok:
                counts["still_pending"] += 1
                continue
            state, _jid, detail = receipts_mod.classify_response(payload)
            if state == "stored":
                with self._lock:
                    self._last_digest[conv_id] = digest
                self._receipts.record(conv_id=conv_id, digest=digest, status="stored",
                                      job_id=rec["job_id"], detail="recovered by reconcile")
                self._receipts.clear_payload(conv_id, digest)
                counts["recovered"] += 1
            elif state == "failed":
                self._receipts.record(conv_id=conv_id, digest=digest, status="failed",
                                      job_id=rec["job_id"], detail=detail or "failed on reconcile")
                counts["failed"] += 1
            else:
                counts["still_pending"] += 1
        resub = 0
        for item in self._receipts.outbox_payloads():
            conv_id, digest = item.get("conv_id"), item.get("digest")
            if not conv_id or (conv_id, digest) in pending_jobs:
                continue  # pending jobs were handled above; don't double-send
            with self._lock:
                if self._last_digest.get(conv_id) == digest:
                    self._receipts.clear_payload(conv_id, digest)
                    continue
            if resub >= max_resubmits:
                break
            state, _detail = self._submit(item.get("messages") or [], conv_id, digest)
            resub += 1
            if state == "stored":
                counts["recovered"] += 1
        counts["resubmitted"] = resub
        return counts

    def unconfirmed_count(self) -> int:
        return len(self._receipts.unresolved()) if self._receipts else 0

    def invalidate(self, conv_id: str) -> None:
        """Forget the stored digest (e.g. after a transcript rewind) so the
        next flush re-ingests even if content happens to match."""
        with self._lock:
            self._last_digest.pop(conv_id, None)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _run_json(
        self, cmd: Sequence[str], *, timeout: float, stdin_text: Optional[str] = None,
    ) -> tuple[bool, Any, str]:
        """Like ``_run`` but hands back the parsed JSON body (or None) so the
        caller can inspect job status — exit code 0 alone proves nothing."""
        if self._runner is None:
            xmem = resolve_xmem()
            if xmem is None:
                return False, None, "xmem CLI not found on PATH"
            cmd = [xmem, *cmd[1:]]
        run_fn: Runner = self._runner or _default_runner
        try:
            code, out, err = run_fn(cmd, timeout, stdin_text)
        except Exception as e:  # noqa: BLE001 — never propagate into the agent
            return False, None, f"xmem call failed: {type(e).__name__}: {e}"
        if code != 0:
            return False, None, f"xmem error: {(err or out).strip() or 'exit ' + str(code)}"
        out = (out or "").strip()
        if not out:
            return True, None, ""
        try:
            return True, json.loads(out), ""
        except ValueError:
            return True, None, ""

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


