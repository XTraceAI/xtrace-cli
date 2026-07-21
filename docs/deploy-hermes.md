# Deploy: Hermes-agent + XTrace memory

End-to-end runbook for running [Nous Research
hermes-agent](https://github.com/nousresearch/hermes-agent) with XTrace hosted
memory as its memory backend, via the `xmem` CLI. ~10 minutes.

## 0. Prerequisites

- hermes-agent installed and working (`hermes` on PATH, `~/.hermes/` present).
- Python 3.10+.
- An XTrace org **API key** (starts with `xtk_`) and the memory API base URL
  (staging: `https://api.staging.xtrace.ai`).

## 1. Install the CLI

```bash
pip install "xtrace-cli @ git+https://github.com/XTraceAI/xtrace-cli.git"
xmem --version
```

## 2. Configure

```bash
xmem config set \
  --api-key   xtk_your_key \
  --base-url  https://api.staging.xtrace.ai \
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

Install the Hermes plugin (adds two model-callable tools + a skill):

```bash
xmem hermes install-plugin               # → ~/.hermes/plugins/xmem/  (+ skill)
# restart hermes so it discovers the plugin
```

The agent now has:
- **`xmem_search(query)`** — pull relevant facts/decisions mid-run.
- **`xmem_recall(tool, file?, entities?, task?)`** — surface procedural
  directives *before* a risky tool action.

The bundled `xmem-memory` skill tells the model when to call them. Both handlers
shell out to `xmem`, so the plugin needs the CLI on PATH and a configured key
(step 2). See `xtrace_cli/integrations/hermes/README.md` for the MemoryProvider /
middleware / MCP alternatives.

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
| `xmem_search`/`xmem_recall` missing in Hermes | plugin not discovered — confirm `~/.hermes/plugins/xmem/` exists and restart hermes |
| plugin tools return "xmem CLI not found" | the CLI isn't on the PATH Hermes runs with — install it in the same env |
| `Hermes state DB not found` | pass `--db <path>` or set `HERMES_HOME` |

## 7. Uninstall

```bash
rm -rf ~/.hermes/plugins/xmem ~/.hermes/skills/xmem   # remove the in-loop tools
pip uninstall xtrace-cli                               # remove the CLI
```
