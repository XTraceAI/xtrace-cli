"""Nous Research hermes-agent integration — the first reference agent.

``session_ingest`` reads a completed Hermes session out of its local SQLite
store (``~/.hermes/state.db``) and maps each turn to the XTrace ingest message
contract. ``cli`` exposes it as the ``xmem hermes`` subcommand group. The core
CLI/client stay agent-agnostic; everything Hermes-specific lives here.
"""
