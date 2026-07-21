# xtrace-cli

`xmem` — a scriptable CLI for **XTrace hosted memory** (`/v1/memories`).

Agent-agnostic: read/write XTrace memory from a shell, a hook, a cron job, or an
agent's toolset. The first reference integration is
[Nous Research hermes-agent](https://github.com/nousresearch/hermes-agent);
per-agent glue lives under `xtrace_cli/integrations/`.

See the spec: [`docs/specs/xtrace-cli.md`](docs/specs/xtrace-cli.md).

## Install

```bash
pip install "xtrace-cli @ git+https://github.com/XTraceAI/xtrace-cli.git"
# or, for local dev:  uv sync   /   pip install -e .
```

**Deploying with hermes-agent?** Follow the end-to-end runbook:
[`docs/deploy-hermes.md`](docs/deploy-hermes.md).

## Configure

```bash
xmem config set --api-key xtk_… --base-url https://api.staging.xtrace.ai --user-id alice
xmem config show     # API key masked
```

Config lives at `~/.config/xtrace-cli/config.yaml` (written `0600`). Any value can
be overridden per-invocation by an environment variable:
`XTRACE_API_KEY`, `XTRACE_BASE_URL`, `XTRACE_USER_ID`, `XTRACE_AGENT_ID`,
`XTRACE_APP_ID`, `XTRACE_NAMESPACE`. The org is derived from the key server-side.

## Hermes integration

```bash
# Capture: ingest a finished session
xmem hermes sessions                       # list recent sessions (~/.hermes/state.db)
xmem hermes ingest --latest --dry-run      # preview what a session would ingest
xmem hermes ingest --session <id> --namespace <repo>   # ingest via the agentic path

# In-loop: install the plugin (xmem_search + xmem_recall tools + skill)
xmem hermes install-plugin                 # → ~/.hermes/plugins/xmem/ (auto-discovered)
```

Ingest reads Hermes' local SQLite store directly (no hermes install needed).
The plugin's tool handlers shell out to `xmem`, so the agent can search memory
and recall directives mid-run. See
[`xtrace_cli/integrations/hermes/README.md`](xtrace_cli/integrations/hermes/README.md)
for the full wiring guide (plugin, skill, and the MemoryProvider / middleware / MCP
alternatives).

## Status

- **M1 (done):** API client (`client.py`, all 8 endpoints) + config + `xmem config set`.
- **M2 (done):** the 8 memory subcommands (`ingest`, `search`, `recall`, `list`, `get`, `delete`, `job`).
- **M3 (done):** Hermes session ingest — `xmem hermes sessions|ingest`.
- **M4 (done):** Hermes plugin (`xmem_search` + `xmem_recall` tools) + skill + `install-plugin`.
- **M5 (done):** packaging (pip-installable wheel, bundled plugin data) + [deploy runbook](docs/deploy-hermes.md).
