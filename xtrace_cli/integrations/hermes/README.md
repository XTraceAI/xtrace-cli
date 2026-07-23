# Hermes ↔ XTrace memory

Wiring [Nous Research hermes-agent](https://github.com/nousresearch/hermes-agent)
to XTrace hosted memory via the `xmem` CLI. All integration points use Hermes'
**supported** extension mechanisms — no core patch. Two modes:

| Mode | Install | Recall | Capture | Slot |
|---|---|---|---|---|
| **Additive** (plugin + skill) | `xmem hermes install-plugin` | model-invoked tools | manual (`xmem hermes ingest`, cron/hook) | none — coexists with Honcho/Mem0/etc. |
| **Provider** (full backend) | `xmem hermes install-provider` | automatic per-turn prefetch + the same tools | automatic at session boundaries | occupies Hermes' single external memory-provider slot |

Prereqs on the Hermes host:
```bash
pip install xtrace-cli
xmem config set --api-key xtk_… --user-id <user> --namespace <repo-or-context>
```

## 1. Session-end ingest  (capture)

After a session, push its transcript into memory:
```bash
xmem hermes ingest --latest --namespace <ctx>      # or --session <id>
xmem hermes sessions                                # find a session id
```
Reads `~/.hermes/state.db` directly (read-only). Wire it to fire on session
close via cron or a wrapper around `hermes`. `--dry-run` previews the plan.

## 2 + 3. In-loop recall & search  (the plugin)

Install the bundled Hermes plugin — registers two model-callable tools:

```bash
xmem hermes install-plugin        # → ~/.hermes/plugins/xmem/ (+ skill)
```

- **`xmem_search(query)`** → `xmem search` — semantic memory search mid-run.
- **`xmem_recall(tool, file?, entities?, task?)`** → `xmem recall` — procedural
  directives for an in-flight tool call.

Hermes auto-discovers plugins under `~/.hermes/plugins/` on next start
(`hermes_cli/plugins.py`). The handlers shell out to the `xmem` CLI, so the
plugin has no Python dependency on this package. The installed **xtrace-memory
skill** nudges the model to call `xmem_recall` before risky tool actions and
`xmem_search` when it needs context.

This is the **additive** path: it doesn't take Hermes' single external
memory-provider slot, so it coexists with Honcho/Mem0/etc. Hermes ≥0.19 also
requires an explicit `hermes plugins enable xmem` after install.

## Provider mode (full backend)

```bash
xmem hermes install-provider          # → ~/.hermes/plugins/xtrace/
# ~/.hermes/config.yaml:
#   memory:
#     provider: xtrace
```

Implements Hermes' `MemoryProvider` ABC (`agent/memory_provider.py`),
registered via `ctx.register_memory_provider(...)` and activated by the
`memory.provider` config key. What it wires:

| Hook | What XTrace does with it |
|---|---|
| `prefetch(query)` | `xmem search` injected into **every** turn, unasked (mode knob: `retrieve` fast / `compose` richer) |
| `sync_turn(…, messages)` | keeps the latest transcript snapshot (no network) |
| `on_session_end(messages)` | ingests the full session — no cron/hook/wrapper needed |
| `on_pre_compress(messages)` | ingests turns about to be discarded by context compression |
| `on_session_switch` / `shutdown` | flushes the old conversation on `/reset`-style switches and on crash-ish exits |
| `get_tool_schemas()` | still exposes `xmem_search` / `xmem_recall` |

Ingest is idempotent per `conv_id` (= the Hermes session id) with a
fingerprint guard, so boundary flushes never double-post an unchanged
transcript. Handlers shell out to `xmem` — same zero-dependency install story
as the plugin. Knobs under `memory.xtrace:` in config.yaml — `prefetch`,
`prefetch_mode`, `auto_ingest`, `namespace`.

Trade-off: the external-provider slot is **exclusive** — pick this mode when
XTrace is the memory backend; pick the additive plugin to coexist with another
provider. Don't run both (duplicate tool names); `hermes plugins disable xmem`
when switching to provider mode.

### How it maps onto Hermes internals

| Point | Hermes mechanism | Where |
|---|---|---|
| Custom tools | plugin `register(ctx)` + `ctx.register_tool(...)`, handler = subprocess | `hermes_cli/plugins.py`, `tools/registry.py` |
| Skill nudge (tier-1 index each turn) | `~/.hermes/skills/xmem/SKILL.md` | `tools/skills_tool.py`, `agent/prompt_builder.py` |

## Alternatives (documented, not shipped)

- **Deterministic pre-tool recall** — register `tool_request` middleware
  (`hermes_cli/middleware.py`) to run `xmem recall` and inject directives into a
  tool's args before it executes. Hermes' `pre_tool_call` hook is block/approve
  only, so it can't inject-and-proceed; middleware is the real interceptor.
- **MCP** — expose `xmem` as an MCP server and add it under `mcp_servers:` in
  `~/.hermes/config.yaml`. Additive, but MCP tools are only model-invoked (no
  pre-tool interception), same as the plugin tools.

## Uninstall
```bash
rm -rf ~/.hermes/plugins/xmem ~/.hermes/skills/xmem    # additive mode
rm -rf ~/.hermes/plugins/xtrace                         # provider mode
# and drop `memory.provider: xtrace` from ~/.hermes/config.yaml
```
