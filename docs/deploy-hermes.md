# Deploy: Hermes-agent + XTrace memory

End-to-end runbook for running [Nous Research
hermes-agent](https://github.com/nousresearch/hermes-agent) with XTrace hosted
memory as its memory backend, via the `xmem` CLI. ~10 minutes.

## 0. Prerequisites

- hermes-agent installed and working (`hermes` on PATH, `~/.hermes/` present).
- Python 3.10+.
- An XTrace org **API key** (starts with `xtk_`) and the memory API base URL
  (the CLI defaults to production; staging: `https://api.staging.xtrace.ai`).

## 1. Install the CLI

```bash
pip install "xtrace-cli @ git+https://github.com/XTraceAI/xtrace-cli.git"
xmem --version
```

## 2. Configure

```bash
xmem config set \
  --api-key   xtk_your_key \
  --user-id   <the user these memories belong to> \
  --namespace <repo / customer / service this deployment works in>
xmem config show          # api key is masked
```

Config lands in `~/.config/xtrace-cli/config.yaml` (mode `0600`). In containers
you can skip the file and pass `XTRACE_API_KEY` / `XTRACE_BASE_URL` /
`XTRACE_USER_ID` / `XTRACE_NAMESPACE` as env vars instead.

Smoke-test connectivity (read-only, safe):
```bash
xmem search "hello" --mode retrieve      # 200 + (likely) no results is success
```

## 3. Capture — ingest finished sessions

After a Hermes session ends, push its transcript into memory:

```bash
xmem hermes sessions                     # list recent session ids
xmem hermes ingest --latest --dry-run    # preview the plan (no API call)
xmem hermes ingest --latest              # ingest the newest session (agentic path)
```

- Reads `~/.hermes/state.db` read-only — no data leaves the box except the
  ingest POST.
- `conv_id` defaults to the Hermes session id; `--namespace` scopes captured
  `lesson`/`procedure` directives.
- `--wait` blocks until the async job finishes; `--no-tools` /
  `--include-system` tune which turns are sent.

**Automate it.** Fire ingest when a session closes — e.g. a cron every few
minutes ingesting the latest, or a wrapper around `hermes`:

```bash
# cron: every 10 min, ingest the most recent session (idempotent per conv_id)
*/10 * * * * XTRACE_API_KEY=xtk_… /usr/local/bin/xmem hermes ingest --latest >> /var/log/xmem.log 2>&1
```

## 4. In-loop — recall & search during a run

Two modes — pick one:

- **Additive plugin** (this section): model-callable tools; coexists with any
  other memory provider; capture stays manual (step 3).
- **Provider mode** (step 4b): XTrace as Hermes' memory backend — automatic
  per-turn prefetch and automatic session ingest; occupies Hermes' single
  external memory-provider slot.

Install the Hermes plugin (adds two model-callable tools + a skill):

```bash
xmem hermes install-plugin               # → ~/.hermes/plugins/xmem/  (+ skill)
hermes plugins enable xmem               # Hermes ≥0.19 requires explicit enable
# restart hermes so it discovers the plugin
```

The agent now has:
- **`xmem_search(query)`** — pull relevant facts/decisions mid-run.
- **`xmem_recall(tool, file?, entities?, task?)`** — surface procedural
  directives *before* a risky tool action.

The bundled `xtrace-memory` skill tells the model when to call them. Both handlers
shell out to `xmem`, so the plugin needs the CLI on PATH and a configured key
(step 2). See `xtrace_cli/integrations/hermes/README.md` for the middleware /
MCP alternatives.

## 4b. Provider mode — XTrace as the memory backend

Instead of (not alongside) the additive plugin:

```bash
hermes plugins disable xmem              # if the additive plugin was installed
xmem hermes install-provider             # → ~/.hermes/plugins/xtrace/
```

Then activate it in `~/.hermes/config.yaml` and restart hermes:

```yaml
memory:
  provider: xtrace
```

What changes vs. the plugin:

- **Recall is automatic** — relevant memories are prefetched into every turn;
  the model can still call `xmem_search` / `xmem_recall` explicitly.
- **Capture is automatic** — the session is ingested at its boundaries
  (session end, pre-compression, resets, shutdown). Step 3's cron/wrapper is
  unnecessary; `conv_id` = the Hermes session id, idempotently.
- **The slot is exclusive** — this displaces any other external memory
  provider (Honcho, Mem0, …). Hermes' built-in local memory is separate and
  unaffected.

Optional knobs under `memory.xtrace:` — `prefetch: false` (tools only),
`prefetch_mode: compose` (richer, slower than the default `retrieve`),
`auto_ingest: false` (recall only), `include_tools: true` (also capture tool
calls/results — **off by default** since 0.2.1 because tool I/O often carries
secrets; opt in for deeper procedural recall), `namespace: <ctx>` (override
the xmem config default).

## 5. Verify the loop

```bash
# 1. ingest a session that touched a file, under a namespace
xmem hermes ingest --latest --namespace demo-repo --wait
# 2. recall directives for a tool call on that file
xmem recall --tool Edit --arg file_path=<a file from the session> \
            --namespace demo-repo --task "…" 
# 3. in Hermes, confirm xmem_search / xmem_recall appear as tools and return results
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `No API key configured` | run `xmem config set --api-key xtk_…` or set `XTRACE_API_KEY` |
| `401` on any call | key/org mismatch — confirm the `xtk_` key matches the base URL's env |
| `A scope is required` | pass `--user-id` (or set a config default) |
| `xmem_search`/`xmem_recall` missing in Hermes | plugin not discovered/enabled — confirm `~/.hermes/plugins/xmem/` exists, run `hermes plugins enable xmem`, restart hermes |
| provider not active (`hermes memory` shows another/builtin) | `memory.provider: xtrace` missing from config.yaml, or `~/.hermes/plugins/xtrace/` not installed |
| duplicate xmem tools | both plugin and provider installed — `hermes plugins disable xmem` in provider mode |
| plugin tools return "xmem CLI not found" | the CLI isn't on the PATH Hermes runs with — install it in the same env |
| `Hermes state DB not found` | pass `--db <path>` or set `HERMES_HOME` |

## 7. Uninstall

```bash
rm -rf ~/.hermes/plugins/xmem ~/.hermes/skills/xmem   # additive mode
rm -rf ~/.hermes/plugins/xtrace                        # provider mode (+ drop memory.provider from config.yaml)
pip uninstall xtrace-cli                               # remove the CLI
```
