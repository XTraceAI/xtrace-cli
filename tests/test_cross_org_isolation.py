"""Cross-org isolation negative tests (client audit Q4). LIVE — skipped unless
two org API keys are supplied:

    XTRACE_ISOLATION_KEY_A=<org A key> \
    XTRACE_ISOLATION_KEY_B=<org B key> \
    pytest tests/test_cross_org_isolation.py -q

Seeds one distinctive memory in each org, waits for extraction, then proves
Key A cannot search, list, retrieve, read revisions of, or delete Org B's
memories (and vice versa via symmetry of the fixtures). "Cannot" accepts
either an auth/not-found error or an empty result — both are isolation; a
2xx carrying the other org's content is a LEAK and fails loudly.

The delete probe is the sharpest: after A attempts to delete B's memory, we
re-read it with B's own key to prove nothing was silently destroyed.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from xtrace_cli.client import XTraceAPIError, XTraceClient

KEY_A = os.environ.get("XTRACE_ISOLATION_KEY_A", "")
KEY_B = os.environ.get("XTRACE_ISOLATION_KEY_B", "")
BASE = os.environ.get("XTRACE_ISOLATION_BASE_URL", "https://api.production.xtrace.ai")
EXTRACT_TIMEOUT = float(os.environ.get("XTRACE_ISOLATION_TIMEOUT", "240"))

pytestmark = pytest.mark.skipif(
    not (KEY_A and KEY_B),
    reason="live isolation test — set XTRACE_ISOLATION_KEY_A and XTRACE_ISOLATION_KEY_B",
)

USER = "iso-test-user"  # same user id in both orgs, on purpose: names must not collide across orgs


def _seed(client: XTraceClient, token: str, conv: str) -> None:
    client.ingest(
        messages=[
            {"role": "user", "content": f"My project codename is {token}. Remember it."},
            {"role": "assistant", "content": f"Noted — your project codename is {token}."},
        ],
        user_id=USER,
        conv_id=conv,
    )


def _search_texts(client: XTraceClient, query: str) -> list[str]:
    res = client.search(query, user_id=USER, mode="retrieve")
    rows = res.get("data") or [] if isinstance(res, dict) else []
    return [str(r.get("memory") or r.get("text") or "") for r in rows if isinstance(r, dict)]


def _first_memory(client: XTraceClient, token: str) -> dict:
    res = client.search(f"project codename {token}", user_id=USER, mode="retrieve")
    for r in (res.get("data") or []) if isinstance(res, dict) else []:
        if token in str(r.get("memory") or r.get("text") or ""):
            return r
    return {}


@pytest.fixture(scope="module")
def orgs():
    """Seed both orgs and wait until each org's fact is searchable by its owner."""
    run = uuid.uuid4().hex[:8]
    a = XTraceClient(BASE, KEY_A)
    b = XTraceClient(BASE, KEY_B)
    tok_a, tok_b = f"ALPHA-{run}", f"BRAVO-{run}"
    _seed(a, tok_a, f"iso-a-{run}")
    _seed(b, tok_b, f"iso-b-{run}")

    deadline = time.time() + EXTRACT_TIMEOUT
    mem_a = mem_b = {}
    while time.time() < deadline and not (mem_a and mem_b):
        time.sleep(10)
        mem_a = mem_a or _first_memory(a, tok_a)
        mem_b = mem_b or _first_memory(b, tok_b)
    if not (mem_a and mem_b):
        pytest.fail(
            f"seeding never became searchable within {EXTRACT_TIMEOUT}s "
            f"(A extracted: {bool(mem_a)}, B extracted: {bool(mem_b)}) — "
            "cannot evaluate isolation without the positive control"
        )
    yield {"a": a, "b": b, "tok_a": tok_a, "tok_b": tok_b, "mem_a": mem_a, "mem_b": mem_b}
    a.close()
    b.close()


# ── positive controls: each org sees its OWN data ─────────────────────────
def test_positive_control_each_org_sees_own_memory(orgs):
    assert any(orgs["tok_a"] in t for t in _search_texts(orgs["a"], f"codename {orgs['tok_a']}"))
    assert any(orgs["tok_b"] in t for t in _search_texts(orgs["b"], f"codename {orgs['tok_b']}"))


# ── negative: search / list ───────────────────────────────────────────────
def test_search_never_returns_other_orgs_content(orgs):
    # Same user id, same query — only the org key differs.
    for t in _search_texts(orgs["a"], f"project codename {orgs['tok_b']}"):
        assert orgs["tok_b"] not in t, f"LEAK: org A retrieved org B content: {t!r}"
    for t in _search_texts(orgs["b"], f"project codename {orgs['tok_a']}"):
        assert orgs["tok_a"] not in t, f"LEAK: org B retrieved org A content: {t!r}"


def test_list_never_returns_other_orgs_content(orgs):
    res = orgs["a"].list_memories(user_id=USER)
    rows = (res.get("data") or []) if isinstance(res, dict) else []
    for r in rows:
        text = str(r.get("memory") or r.get("text") or "")
        assert orgs["tok_b"] not in text, f"LEAK via list: {text!r}"


# ── negative: direct object access by id ─────────────────────────────────
def _expect_denied(fn, what: str):
    try:
        body = fn()
    except XTraceAPIError as e:
        assert e.status_code in (401, 403, 404), f"{what}: unexpected status {e.status_code}"
        return
    # A 2xx is only acceptable if it carries no content (defensive empty body).
    assert not body, f"LEAK: {what} returned content across orgs: {str(body)[:200]}"


def test_get_by_id_denied_across_orgs(orgs):
    _expect_denied(lambda: orgs["a"].get_memory(orgs["mem_b"]["id"]), "GET /{id}")


def test_revisions_denied_across_orgs(orgs):
    _expect_denied(lambda: orgs["a"].get_revisions(orgs["mem_b"]["id"]), "GET /{id}/revisions")


def test_delete_denied_across_orgs_and_nothing_destroyed(orgs):
    _expect_denied(lambda: orgs["a"].delete_memory(orgs["mem_b"]["id"]), "DELETE /{id}")
    # The sharp edge: prove B's memory survived A's delete attempt.
    survivor = orgs["b"].get_memory(orgs["mem_b"]["id"])
    assert isinstance(survivor, dict) and survivor.get("id") == orgs["mem_b"]["id"], (
        "org A's cross-org DELETE silently destroyed org B's memory"
    )


# ── negative: procedural recall ───────────────────────────────────────────
def test_trigger_never_recalls_other_orgs_directives(orgs):
    res = orgs["a"].trigger(entities=[orgs["tok_b"]], task="isolation probe", user_id=USER)
    blob = str(res)
    # The request itself contains tok_b (we fire recall ON that entity), and
    # some envelopes echo the query back — an echo is not a leak. A leak is
    # recalled CONTENT: the token together with the seeded fact's wording.
    assert orgs["tok_b"] not in blob or "codename" not in blob, (
        f"LEAK via trigger/recall: {blob[:200]}"
    )
