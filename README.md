# xtrace-cli

`xmem` — a scriptable CLI and agent memory provider for
**[XTrace](https://xtrace.ai) hosted memory** (`/v1/memories`).

Give an AI agent long-term memory it can prove: semantic recall injected into
every turn, automatic session capture with **storage receipts**, and
procedural directives ("last time you touched this file, X broke") recalled
right before risky actions.

Agent-agnostic core: read/write XTrace memory from a shell, a hook, a cron
job, or any agent's toolset. Per-agent glue lives under
`xtrace_cli/integrations/` — the reference integration is
[Nous Research hermes-agent](https://github.com/nousresearch/hermes-agent).

## Install

```bash
pip install "xtrace-cli @ git+https://github.com/XTraceAI/xtrace-cli.git"
xmem --version
```

You'll need an XTrace org API key. Configure once:

```bash
xmem config set --api-key <key> --user-id <person> --namespace <project>
xmem search "hello" --mode retrieve    # connectivity check — 200 = good
```

Config lives at `~/.config/xtrace-cli/config.yaml` (written `0600`). Every
value can also come from environment variables (`XTRACE_API_KEY`,
`XTRACE_USER_ID`, `XTRACE_NAMESPACE`, …) — handy in containers. The org is
derived from the key server-side; one key = one isolated org.

## The CLI

Eight commands mapping 1:1 onto the memory API:

```bash
xmem ingest --stdin --conv-id <id>      # extract memories from a transcript
xmem job <job_id>                       # poll an async ingest job
xmem search "query" --mode retrieve     # semantic search (compose = prompt-ready)
xmem recall --tool Edit --arg file_path=app.py   # procedural directives, pre-action
xmem list / get / revisions / delete    # inspect and manage stored memories
```

## Hermes integration

Two modes:

| Mode | Install | What you get |
|---|---|---|
| **Provider** (recommended) | `xmem hermes install-provider` + `memory.provider: xtrace` in `~/.hermes/config.yaml` | automatic per-turn recall, automatic session capture with receipts, plus the `xmem_search` / `xmem_recall` tools |
| **Additive plugin** | `xmem hermes install-plugin` + `hermes plugins enable xmem` | model-invoked tools only; coexists with another memory provider |

End-to-end runbook (including the verified Docker recipe):
[`docs/deploy-hermes.md`](docs/deploy-hermes.md). Integration internals:
[`xtrace_cli/integrations/hermes/README.md`](xtrace_cli/integrations/hermes/README.md).

## Capture assurance

"Accepted" is not "stored" — this provider treats that distinction as the
whole job:

- Every capture is polled to a **terminal state**; "stored" is only reported
  (and deduplication only committed) after the server proves storage.
- Every submission writes a **durable receipt** — deterministic SHA-256
  payload digest, job id, final status — under `~/.hermes/xtrace/`.
- Anything unproven survives restarts in an **on-disk outbox** and is
  reconciled on the next start. `xmem hermes receipts` shows the ledger;
  `--flush` reconciles now.
- Failures are surfaced to the user through the agent itself, not buried in
  a log file.
- Tool calls/results are **excluded from capture by default** (tool I/O often
  carries secrets); opt in per deployment for deeper procedural recall.

A live cross-org isolation test suite ships in
[`tests/test_cross_org_isolation.py`](tests/test_cross_org_isolation.py) —
point it at two throwaway orgs and it proves neither key can read, revise, or
delete the other's memories.

## Development

```bash
uv sync                      # or: pip install -e ".[dev]"
uv run pytest -q             # unit tests (live suites skip without keys)
uv run ruff check .
```

## License

See [LICENSE](LICENSE).
