---
name: xtrace-memory
description: |
  Use XTrace long-term memory. Call xmem_search to pull relevant facts and
  past decisions when you lack context, and call xmem_recall BEFORE a risky
  tool action (editing a file, running a command, deploying) to surface
  procedural directives — lessons and procedures past sessions recorded about
  the tool or files you're about to touch.
version: 0.1.0
metadata:
  hermes:
    tags: [memory, xtrace, procedural-memory]
    category: memory
---

# XTrace memory (xmem)

You have two memory tools backed by XTrace:

- **`xmem_search(query)`** — semantic search over long-term memory (facts,
  decisions, prior context). Call it when you need background you don't already
  have in the conversation.
- **`xmem_recall(tool, file?, entities?, task?)`** — procedural-memory recall.
  It fires on a *symbol tripwire*: pass the `tool` you're about to run and the
  `file`/`entities` it touches, and it returns `lesson`/`procedure` directives
  that earlier sessions recorded about exactly those symbols — what failed and
  the path that worked.

## When to use `xmem_recall`

Call it **before** a consequential tool action, not after:

- editing or creating a file → `xmem_recall(tool="Edit", file="<path>", task="<goal>")`
- running a command / script → `xmem_recall(tool="Shell", entities=["<cmd or symbol>"])`
- deploying, migrating, or touching infra → recall on the service/resource name

Treat the returned directives as **advisory guardrails**: they flag known
pitfalls and preferred procedures. If a directive is relevant, follow it; if it
looks stale, note that and proceed.

## When to use `xmem_search`

When you need context the current conversation doesn't contain — a past
decision, a preference, how something was set up. Use a short, direct query.
