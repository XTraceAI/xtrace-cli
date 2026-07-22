# Hermes ↔ XTrace memory

Wiring [Nous Research hermes-agent](https://github.com/nousresearch/hermes-agent)
to XTrace hosted memory via the `xmem` CLI. Three integration points; all use
Hermes' **supported** extension points — no core patch.

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
memory-provider slot, so it coexists with Honcho/Mem0/etc.

### How it maps onto Hermes internals

| Point | Hermes mechanism | Where |
|---|---|---|
| Custom tools | plugin `register(ctx)` + `ctx.register_tool(...)`, handler = subprocess | `hermes_cli/plugins.py`, `tools/registry.py` |
| Skill nudge (tier-1 index each turn) | `~/.hermes/skills/xmem/SKILL.md` | `tools/skills_tool.py`, `agent/prompt_builder.py` |

## Alternatives (documented, not shipped)

- **Auto per-turn injection** — implement a `MemoryProvider` whose
  `prefetch(query)` shells to `xmem search` so results inject into *every* turn
  without the model asking (`agent/memory_provider.py`,
  `agent/turn_context.py`). Trade-off: the external-provider slot is
  **exclusive** — conflicts with an existing provider. Prefer the plugin unless
  you want unconditional prefetch and run no other provider.
- **Deterministic pre-tool recall** — register `tool_request` middleware
  (`hermes_cli/middleware.py`) to run `xmem recall` and inject directives into a
  tool's args before it executes. Hermes' `pre_tool_call` hook is block/approve
  only, so it can't inject-and-proceed; middleware is the real interceptor.
- **MCP** — expose `xmem` as an MCP server and add it under `mcp_servers:` in
  `~/.hermes/config.yaml`. Additive, but MCP tools are only model-invoked (no
  pre-tool interception), same as the plugin tools.

## Uninstall
```bash
rm -rf ~/.hermes/plugins/xmem ~/.hermes/skills/xmem
```
