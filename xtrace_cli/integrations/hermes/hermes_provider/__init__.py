"""xtrace — XTrace hosted memory provider for hermes-agent.

Full-backend mode: occupies Hermes' single external memory-provider slot
(``memory.provider: xtrace`` in ``~/.hermes/config.yaml``). Compared to the
additive ``xmem`` tools plugin, the provider adds the push side:

- ``prefetch``            — XTrace context injected into every turn, unasked.
- ``sync_turn``           — keeps a transcript snapshot for boundary flushes.
- ``on_session_end`` / ``on_pre_compress`` / ``on_session_switch`` /
  ``shutdown``            — automatic ingest at session boundaries (no cron,
  no shell hook, no wrapper).
- ``xmem_search`` / ``xmem_recall`` — still exposed as model-callable tools.

Install with ``xmem hermes install-provider`` (copies this directory to
``~/.hermes/plugins/xtrace/``). Handlers shell out to the ``xmem`` CLI, so the
installed provider has no Python dependency on this package. Hermes-side knobs
live under ``memory.xtrace`` in config.yaml:

- ``prefetch`` (default true)         — inject recalled context each turn
- ``prefetch_mode`` (default retrieve) — ``retrieve`` (fast) or ``compose``
- ``auto_ingest`` (default true)      — flush transcripts at session boundaries
- ``include_tools`` (default false)   — also ingest tool calls/results (opt in;
  tool output often carries secrets, but it is where procedural lessons live)
- ``reconcile`` (default true)        — on start, poll unproven jobs and
  resubmit failed captures from the on-disk outbox (``$HERMES_HOME/xtrace``)
- ``namespace`` (default unset)       — override the xmem config namespace
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The core is imported relatively when loaded as a package (both inside
# xtrace_cli and as ~/.hermes/plugins/xtrace). Fall back to a by-path load in
# case the loader execs __init__ without full package context.
try:
    from . import _provider_core as core_mod
except ImportError:  # pragma: no cover - defensive for odd loaders
    import importlib.util
    import pathlib

    _p = pathlib.Path(__file__).with_name("_provider_core.py")
    _spec = importlib.util.spec_from_file_location("_xtrace_provider_core", _p)
    core_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(core_mod)  # type: ignore[union-attr]

# Real base class inside hermes; a plain shim outside it (unit tests, linters)
# so this module stays importable anywhere.
try:  # pragma: no cover - exercised only inside hermes
    from agent.memory_provider import MemoryProvider
except ImportError:
    class MemoryProvider:  # type: ignore[no-redef]
        """Stand-in when hermes isn't importable."""

try:  # pragma: no cover - exercised only inside hermes
    from tools.registry import tool_error
except ImportError:
    def tool_error(msg: str) -> str:
        return json.dumps({"error": msg})

_MIN_QUERY_LEN = 10

SEARCH_SCHEMA = {
    "name": "xmem_search",
    "description": (
        "Search XTrace long-term memory for facts, decisions, and past context "
        "relevant to the current task. Use when you need background you don't "
        "already have."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language query."}
        },
        "required": ["query"],
    },
}

RECALL_SCHEMA = {
    "name": "xmem_recall",
    "description": (
        "Recall procedural directives (lessons/procedures) that past sessions "
        "recorded about a tool or files you are about to touch. Call this "
        "BEFORE a risky tool action (edit/run/deploy) to surface known "
        "pitfalls and the path that worked."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "Tool you're about to run (e.g. Edit)."},
            "file": {"type": "string", "description": "File the tool will touch, if any."},
            "entities": {
                "type": "array", "items": {"type": "string"},
                "description": "Explicit symbols/files to fire recall on.",
            },
            "task": {"type": "string", "description": "Your current goal, one line."},
        },
        "required": [],
    },
}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _load_plugin_config() -> Dict[str, Any]:
    """Hermes-side knobs from ``memory.xtrace`` in config.yaml (empty outside
    hermes or when unset)."""
    try:  # pragma: no cover - hermes-only import
        from hermes_cli.config import load_config

        mc = load_config().get("memory", {})
        pc = mc.get("xtrace", {}) if isinstance(mc, dict) else {}
        return dict(pc) if isinstance(pc, dict) else {}
    except Exception:
        return {}


class XTraceMemoryProvider(MemoryProvider):
    """XTrace hosted memory as Hermes' external memory provider."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        core: Optional["core_mod.ProviderCore"] = None,
    ):
        cfg = dict(config) if config is not None else _load_plugin_config()
        self._prefetch_enabled = _coerce_bool(cfg.get("prefetch"), True)
        self._prefetch_mode = str(cfg.get("prefetch_mode") or "retrieve")
        self._auto_ingest = _coerce_bool(cfg.get("auto_ingest"), True)
        self._reconcile_on_start = _coerce_bool(cfg.get("reconcile"), True)
        self._unconfirmed = 0
        # Off by default since 0.2.1: tool output often carries secrets/PII.
        # Opt in (memory.xtrace.include_tools: true) for procedural recall depth.
        self._include_tools = _coerce_bool(cfg.get("include_tools"), False)
        namespace = str(cfg.get("namespace") or "") or None
        self._core = core or core_mod.ProviderCore(namespace=namespace)
        self._session_id = ""
        self._writes_enabled = True
        self._lock = threading.Lock()
        # Transcript snapshots keyed by session id — one provider instance may
        # serve several concurrent sessions (gateway group chats, cached
        # agents), and Hermes passes session_id per call for exactly that.
        self._snapshots: Dict[str, List[Dict[str, Any]]] = {}
        self._bg_threads: list[threading.Thread] = []

    # ── identity / availability ─────────────────────────────────────────
    @property
    def name(self) -> str:
        return "xtrace"

    def is_available(self) -> bool:
        """xmem CLI resolvable (PATH or well-known install dirs — GUI-launched
        Hermes has a bare PATH). No network, per the ABC contract; a missing or
        unconfigured key surfaces as a friendly tool/prefetch message instead."""
        return core_mod.resolve_xmem() is not None

    # ── lifecycle ────────────────────────────────────────────────────────
    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        # Cron/flush/subagent contexts must not write user memories.
        agent_context = str(kwargs.get("agent_context") or "primary")
        self._writes_enabled = agent_context == "primary"
        hermes_home = kwargs.get("hermes_home")
        if hermes_home and self._writes_enabled:
            try:
                store = core_mod.receipts_mod.ReceiptStore(Path(hermes_home) / "xtrace")
                self._core.attach_receipts(store)
                self._refresh_unconfirmed()
                if self._reconcile_on_start:
                    t = threading.Thread(target=self._reconcile, daemon=True,
                                         name="xtrace-reconcile")
                    self._bg_threads.append(t)
                    t.start()
            except Exception:  # pragma: no cover - receipts must never break init
                logger.exception("xtrace receipts init failed")

    def _reconcile(self) -> None:
        try:
            counts = self._core.reconcile()
            if any(counts.values()):
                logger.info("xtrace reconcile: %s", counts)
        except Exception:  # pragma: no cover
            logger.exception("xtrace reconcile failed")
        self._refresh_unconfirmed()

    def _refresh_unconfirmed(self) -> None:
        try:
            self._unconfirmed = self._core.unconfirmed_count()
        except Exception:  # pragma: no cover
            self._unconfirmed = 0

    def system_prompt_block(self) -> str:
        block = (
            "# XTrace memory\n"
            "Long-term memory is active. Relevant facts and past decisions are "
            "injected automatically each turn; the session is captured back to "
            "memory when it ends. For anything beyond the injected context, "
            "call xmem_search. BEFORE a risky tool action (edit/run/deploy), "
            "call xmem_recall to surface lessons and procedures past sessions "
            "recorded about the exact files or symbols you're about to touch."
        )
        if self._unconfirmed:
            # Field-study finding: failures must be visible, not log-only.
            # This puts the fact in front of the user via the agent itself.
            block += (
                f"\nNOTE: {self._unconfirmed} earlier session capture(s) are "
                "unconfirmed (failed or still unproven). If the user asks about "
                "memory reliability, tell them; details: `xmem hermes receipts`."
            )
        return block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Synchronous recall — MemoryManager runs external prefetch in its own
        timeout-guarded thread, so blocking here is safe."""
        if not self._prefetch_enabled:
            return ""
        q = (query or "").strip()
        if len(q) < _MIN_QUERY_LEN:
            return ""
        ok, text = self._core.search(q, mode=self._prefetch_mode)
        if not ok or not text.strip():
            return ""
        return f"## XTrace memory\n{text.strip()}"

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Keep the latest transcript snapshot per session; boundary hooks
        flush them. No network here — sync_turn must stay cheap."""
        if not messages:
            return
        sid = session_id or self._session_id
        if not sid:
            return
        with self._lock:
            self._snapshots[sid] = list(messages)
            # Bound growth for long-lived gateway processes with abandoned
            # sessions: evict oldest-inserted (its only loss is the crash-path
            # safety net; session-end/switch flushes pop entries normally).
            while len(self._snapshots) > 128:
                self._snapshots.pop(next(iter(self._snapshots)))

    def _owning_sid(self, messages: List[Dict[str, Any]]) -> str:
        """Which session does this transcript belong to? on_session_end and
        on_pre_compress carry no session_id, so match the transcript's opening
        turns against stored snapshots (both hand us lists that start at the
        conversation head). Unique match wins; anything else falls back to
        this agent's current session."""
        head = [(m.get("role"), m.get("content")) for m in messages[:2] if isinstance(m, dict)]
        if head:
            with self._lock:
                matches = {
                    sid for sid, snap in self._snapshots.items()
                    if [(m.get("role"), m.get("content")) for m in snap[:2]] == head
                }
            if len(matches) == 1:
                return matches.pop()
        return self._session_id

    # ── capture (the reason the provider exists) ─────────────────────────
    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        sid = self._owning_sid(messages)
        self._flush(messages, sid, wait=True)
        with self._lock:
            self._snapshots.pop(sid, None)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        # Capture turns about to be discarded by compression. Same conv_id as
        # the eventual session-end flush — the ingest path is idempotent per
        # conv_id, so this is an early partial save, not a duplicate.
        self._flush(messages, self._owning_sid(messages), wait=False)
        return ""

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        old_id = self._session_id
        if rewound and not reset:
            # Same session, truncated transcript — forget the fingerprint so
            # the next flush re-ingests the rewritten conversation, and drop
            # the now-stale snapshot.
            self._core.invalidate(old_id)
            with self._lock:
                self._snapshots.pop(old_id, None)
            return
        with self._lock:
            snapshot = self._snapshots.pop(old_id, None)
        if snapshot and old_id and old_id != new_session_id:
            # The old conversation is being left behind — flush it under its
            # own id (captured NOW, not read from self inside the thread).
            self._flush(snapshot, old_id, wait=False)
        self._session_id = new_session_id

    def shutdown(self) -> None:
        with self._lock:
            remaining = list(self._snapshots.items())
            self._snapshots.clear()
        for sid, snapshot in remaining:
            # Crash-path safety net, one flush per still-open session; the
            # fingerprint makes each a no-op when on_session_end already
            # flushed the same transcript.
            self._flush(snapshot, sid, wait=True)
        for t in self._bg_threads:
            if t.is_alive():
                t.join(timeout=5.0)

    def _flush(self, messages: List[Dict[str, Any]], conv_id: str, *, wait: bool) -> None:
        if not (self._auto_ingest and self._writes_enabled):
            return
        if not messages or not conv_id:
            return
        msgs = list(messages)
        include_tools = self._include_tools

        def work() -> None:
            state, detail = self._core.ingest(msgs, conv_id, include_tools=include_tools)
            log = {"stored": logger.info, "skipped": logger.info,
                   "pending": logger.warning}.get(state, logger.error)
            log("xtrace capture (%s): %s — %s", conv_id, state, detail)
            self._refresh_unconfirmed()

        if wait:
            work()
            return
        t = threading.Thread(target=work, daemon=True, name="xtrace-ingest")
        self._bg_threads = [x for x in self._bg_threads if x.is_alive()]
        self._bg_threads.append(t)
        t.start()

    # ── tools (same surface as the additive plugin) ──────────────────────
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, RECALL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        a = args or {}
        if tool_name == "xmem_search":
            query = str(a.get("query") or "").strip()
            if not query:
                return tool_error("query is required")
            ok, text = self._core.search(query, mode="compose")
            if not ok:
                return tool_error(text)
            return json.dumps({"result": text or "No relevant memories found."})
        if tool_name == "xmem_recall":
            if not a.get("tool") and not a.get("entities"):
                return tool_error("Provide 'tool' (and optional 'file') or 'entities'.")
            ok, text = self._core.recall(
                tool=a.get("tool"), file=a.get("file"),
                entities=a.get("entities"), task=a.get("task"),
            )
            if not ok:
                return tool_error(text)
            return json.dumps({"result": text or "No directives recorded for this action."})
        return tool_error(f"Unknown tool: {tool_name}")

    # ── setup / ops integration ──────────────────────────────────────────
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "XTrace org API key (xtk_…)",
                "secret": True,
                "required": True,
                "env_var": "XTRACE_API_KEY",
                "url": "https://xtrace.ai",
            },
            {"key": "user_id", "description": "User these memories belong to", "required": True},
            {"key": "namespace", "description": "Working context (repo / customer / service)"},
            {"key": "base_url", "description": "Memory API base URL (blank = xmem config default)"},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret fields via ``xmem config set`` so the CLI and the
        provider read the same config (the key itself goes to .env)."""
        cmd = [core_mod.XMEM_BIN, "config", "set"]
        for key, flag in (("user_id", "--user-id"), ("namespace", "--namespace"), ("base_url", "--base-url")):
            v = str(values.get(key) or "").strip()
            if v:
                cmd += [flag, v]
        if len(cmd) > 3:
            self._core._run(cmd, timeout=15.0)

    def backup_paths(self) -> List[str]:
        return [str(Path.home() / ".config" / "xtrace-cli" / "config.yaml")]


def register(ctx) -> None:
    """Plugin entry point — hermes' memory discovery calls this with a context
    that captures ``register_memory_provider``."""
    ctx.register_memory_provider(XTraceMemoryProvider())
