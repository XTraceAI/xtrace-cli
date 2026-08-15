"""Durable capture receipts + outbox for the XTrace Hermes provider.

Pure stdlib, no hermes imports — unit-testable and copied verbatim into
``~/.hermes/plugins/xtrace``. Answers the field-study finding that "accepted
is not stored": every submission gets a durable receipt with a deterministic
payload digest and a terminal status, and anything unproven survives process
death in an on-disk outbox for reconciliation on the next start.

Layout under ``<base>/`` (default ``$HERMES_HOME/xtrace/``):

- ``receipts.jsonl`` — append-only ledger: one record per submission attempt
  and per later resolution. Fields: ``ts``, ``conv_id``, ``digest``,
  ``turns``, ``job_id``, ``status`` (stored | pending | failed | skipped),
  ``detail``.
- ``outbox/<conv>-<digest12>.json`` — full mapped payload for anything not
  proven stored, so it can be resubmitted or re-polled after a restart.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable, Optional

TERMINAL_SUCCESS = {"completed", "succeeded", "success", "done"}
TERMINAL_FAILURE = {"failed", "error", "cancelled"}


def payload_digest(mapped: Iterable[dict]) -> str:
    """Deterministic sha256 over the canonical mapped transcript — stable
    across processes and restarts (unlike ``hash()``)."""
    canon = json.dumps(list(mapped), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class ReceiptStore:
    """Append-only receipts ledger + outbox. Never raises into the caller."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.receipts_path = self.base / "receipts.jsonl"
        self.outbox_dir = self.base / "outbox"

    # ── writes ───────────────────────────────────────────────────────────
    def record(
        self,
        *,
        conv_id: str,
        digest: str,
        status: str,
        turns: int = 0,
        job_id: Optional[str] = None,
        detail: str = "",
    ) -> None:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "conv_id": conv_id,
            "digest": digest,
            "turns": turns,
            "job_id": job_id,
            "status": status,
            "detail": detail[:500],
        }
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            with self.receipts_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def stash_payload(self, conv_id: str, digest: str, mapped: list[dict]) -> None:
        """Keep the full payload for anything not proven stored."""
        try:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            path = self.outbox_dir / f"{_safe(conv_id)}-{digest[:12]}.json"
            path.write_text(json.dumps({"conv_id": conv_id, "digest": digest,
                                        "messages": mapped}), encoding="utf-8")
        except OSError:
            pass

    def clear_payload(self, conv_id: str, digest: str) -> None:
        try:
            (self.outbox_dir / f"{_safe(conv_id)}-{digest[:12]}.json").unlink(missing_ok=True)
        except OSError:
            pass

    # ── reads ────────────────────────────────────────────────────────────
    def records(self) -> list[dict]:
        try:
            lines = self.receipts_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def last_stored_digests(self) -> dict[str, str]:
        """conv_id → digest of the most recent PROVEN-stored submission.
        This is the cross-restart dedup fingerprint."""
        out: dict[str, str] = {}
        for r in self.records():
            if r.get("status") == "stored" and r.get("conv_id"):
                out[r["conv_id"]] = r.get("digest", "")
        return out

    def unresolved(self) -> list[dict]:
        """Latest record per (conv, digest) whose status is pending/failed —
        i.e. submissions with no later 'stored' or 'skipped' resolution."""
        latest: dict[tuple, dict] = {}
        for r in self.records():
            key = (r.get("conv_id"), r.get("digest"))
            latest[key] = r
        return [r for r in latest.values() if r.get("status") in ("pending", "failed")]

    def outbox_payloads(self) -> list[dict]:
        try:
            files = sorted(self.outbox_dir.glob("*.json"))
        except OSError:
            return []
        out = []
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["_path"] = str(p)
                out.append(data)
            except (OSError, ValueError):
                continue
        return out


def classify_response(payload: Any) -> tuple[str, Optional[str], str]:
    """An ingest/job JSON response → (state, job_id, detail).

    state: 'stored' | 'pending' | 'failed'. Trusts the body over the exit
    code — a 0-exit response whose body says failed IS a failure (field-study
    finding: Pearl's synthetic test).
    """
    if not isinstance(payload, dict):
        return "stored", None, ""  # non-envelope success body (sync path)
    # The live envelope carries the id under "id" (job objects), older shapes
    # under "job_id" — accept both, same as the CLI's own --wait path.
    job_id = payload.get("job_id") or payload.get("id")
    status = str(payload.get("status") or "").lower()
    detail = str(payload.get("error") or payload.get("detail") or "")[:300]
    if status in TERMINAL_FAILURE:
        return "failed", job_id, detail or f"job status {status}"
    if status in TERMINAL_SUCCESS or not status:
        return "stored", job_id, ""
    return "pending", job_id, f"job status {status}"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:80]
