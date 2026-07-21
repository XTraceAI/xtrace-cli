"""xmem — XTrace hosted memory plugin for hermes-agent.

Registers two model-callable tools that shell out to the ``xmem`` CLI:

- ``xmem_search`` — semantic search of XTrace long-term memory.
- ``xmem_recall`` — procedural-memory (lesson/procedure) recall for a tool call.

Install with ``xmem hermes install-plugin`` (copies this directory to
``~/.hermes/plugins/xmem/``, which Hermes auto-discovers). Requires the ``xmem``
CLI on PATH and a configured key (``xmem config set --api-key …`` or
``XTRACE_API_KEY``). No hermes core patch — uses the supported plugin
``register(ctx)`` + ``ctx.register_tool`` extension point.
"""

from __future__ import annotations

# The bridge is imported relatively when loaded as a package (both inside
# xtrace_cli and as ~/.hermes/plugins/xmem). Fall back to a by-path load in case
# the loader execs __init__ without full package context.
try:
    from ._bridge import build_recall_cmd, build_search_cmd, run
except ImportError:  # pragma: no cover - defensive for odd loaders
    import importlib.util
    import pathlib

    _p = pathlib.Path(__file__).with_name("_bridge.py")
    _spec = importlib.util.spec_from_file_location("_xmem_bridge", _p)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    build_recall_cmd, build_search_cmd, run = (
        _mod.build_recall_cmd, _mod.build_search_cmd, _mod.run,
    )

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


def _search_handler(args: dict, **_kw) -> str:
    query = (args or {}).get("query", "").strip()
    if not query:
        return "Provide a 'query'."
    return run(build_search_cmd(query))


def _recall_handler(args: dict, **_kw) -> str:
    a = args or {}
    if not a.get("tool") and not a.get("entities"):
        return "Provide 'tool' (and optional 'file') or 'entities' to fire recall."
    return run(build_recall_cmd(
        tool=a.get("tool"), file=a.get("file"),
        entities=a.get("entities"), task=a.get("task"),
    ))


def register(ctx) -> None:
    """Plugin entry point — called once by the Hermes plugin loader."""
    ctx.register_tool(
        name="xmem_search", toolset="xmem", schema=SEARCH_SCHEMA,
        handler=_search_handler, emoji="🧠",
        description="Search XTrace long-term memory.",
    )
    ctx.register_tool(
        name="xmem_recall", toolset="xmem", schema=RECALL_SCHEMA,
        handler=_recall_handler, emoji="🧠",
        description="Recall procedural directives for a tool call.",
    )
