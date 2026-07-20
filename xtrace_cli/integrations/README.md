# Agent integrations

The core (`xtrace_cli.client` / `.cli` / `.config`) is agent-agnostic. Each
subdirectory here wires one agent runtime to `xmem` at the three integration
points from the spec:

1. **Session-end ingest** — map the agent's session store to the `{role,
   content, date?}` message contract and call `xmem ingest --agentic`.
2. **Pre-tool-call recall** — call `xmem recall` on the in-flight tool call.
3. **In-loop search** — expose `xmem search` as a toolset entry.

Adding an agent = a new folder here (a session→messages adapter + wiring docs).
No core changes. First reference: `hermes/` (Nous Research hermes-agent).
