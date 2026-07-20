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

## Status

- **M1 (done):** API client (`client.py`, all 8 endpoints) + config + `xmem config set`.
- **M2:** the 8 memory subcommands (`ingest`, `search`, `recall`, `list`, `get`, `delete`, `job`).
- **M3–M4:** Hermes session ingest + recall/search glue.
- **M5:** packaging + customer runbook.
