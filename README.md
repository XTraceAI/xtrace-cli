# xtrace-cli

`xmem` — a scriptable CLI for **XTrace hosted memory** (`/v1/memories`).

Agent-agnostic: read/write XTrace memory from a shell, a hook, a cron job, or an
agent's toolset. The first reference integration is
[Nous Research hermes-agent](https://github.com/nousresearch/hermes-agent);
per-agent glue lives under `xtrace_cli/integrations/`.

See the spec: [`docs/specs/xtrace-cli.md`](docs/specs/xtrace-cli.md).

## Install

```bash
uv sync              # or: pip install -e .
```

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
xmem hermes sessions                       # list recent hermes-agent sessions (~/.hermes/state.db)
xmem hermes ingest --latest --dry-run      # preview what a session would ingest
xmem hermes ingest --session <id> --namespace <repo>   # ingest via the agentic path
```

Reads Hermes' local SQLite store directly (no hermes install needed). `--db` points
at a non-default `state.db`; `--no-tools` / `--include-system` tune which turns go in.

## Status

- **M1 (done):** API client (`client.py`, all 8 endpoints) + config + `xmem config set`.
- **M2 (done):** the 8 memory subcommands (`ingest`, `search`, `recall`, `list`, `get`, `delete`, `job`).
- **M3 (done):** Hermes session ingest — `xmem hermes sessions|ingest`.
- **M4:** Hermes pre-tool-call recall + in-loop search wiring (skill/toolset docs).
- **M5:** packaging + customer runbook.
