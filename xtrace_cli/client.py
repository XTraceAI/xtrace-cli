"""Typed HTTP client for the XTrace hosted memory API (``/v1/memories``).

Covers all 8 endpoints. Agent-agnostic — knows nothing about Hermes. The CLI
layer (``cli.py``) and any integration adapter build requests through this.

Auth: sent as ``Authorization: Token <api-key>`` (the server also accepts
``x-api-key`` and ``Authorization: Bearer``; org is derived from the key, so no
``X-Org-Id`` is sent — it is deprecated).
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Sequence

import httpx

Mode = Literal["compose", "retrieve"]
MemoryType = Literal["fact", "episode", "artifact", "lesson", "procedure"]


def _user_agent() -> str:
    try:
        from xtrace_cli import __version__
        return f"xmem/{__version__}"
    except Exception:  # pragma: no cover - version must never break requests
        return "xmem"


class XTraceAPIError(Exception):
    """A non-2xx response from the memory API, with status and parsed body."""

    def __init__(self, status_code: int, body: Any, method: str, url: str):
        self.status_code = status_code
        self.body = body
        self.method = method
        self.url = url
        detail = body
        if isinstance(body, dict):
            # The API returns an {error: {code, message}} envelope on failure.
            err = body.get("error") or body.get("detail") or body
            detail = err.get("message") if isinstance(err, dict) else err
        super().__init__(f"{method} {url} → {status_code}: {detail}")


class XTraceClient:
    """Synchronous client. Use as a context manager or call ``close()``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    # ── lifecycle ────────────────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "XTraceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── low-level request ────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Identify ourselves: the default python-httpx UA is a stock
            # target for edge/WAF bot rules (field-observed 403s at the CDN).
            "User-Agent": _user_agent(),
        }

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self._base}/v1/memories{path}"
        resp = self._client.request(method, url, headers=self._headers(), **kw)
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        if 300 <= resp.status_code < 400:
            # The server 307s trailing-slash paths with an EMPTY body (and a
            # Location that downgrades to http). Treating that as success made
            # ingest/list silent no-ops — always surface redirects as errors.
            raise XTraceAPIError(
                resp.status_code,
                {"error": {"message": f"unexpected redirect to {resp.headers.get('location')}"}},
                method, url,
            )
        if resp.status_code >= 400:
            raise XTraceAPIError(resp.status_code, body, method, url)
        return body

    # ── 1. ingest ────────────────────────────────────────────────────────
    def ingest(
        self,
        *,
        messages: Sequence[dict],
        user_id: str,
        conv_id: str,
        agent_id: str | None = None,
        app_id: str | None = None,
        namespace: str | None = None,
        group_ids: Sequence[str] | None = None,
        agentic: bool = False,
        extract_artifacts: bool = False,
        timestamp_format: str | None = None,
    ) -> Any:
        """POST /v1/memories/ — extract memories from a message list.

        Returns the ingest result (or a job envelope with a ``job_id`` when the
        server routes to the async/batch path). ``messages`` are ``{role,
        content, date?, dia_id?}`` dicts; order matters.
        """
        payload: dict[str, Any] = {
            "messages": list(messages),
            "user_id": user_id,
            "conv_id": conv_id,
            "agentic": agentic,
            "extract_artifacts": extract_artifacts,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        if app_id:
            payload["app_id"] = app_id
        if namespace:
            payload["namespace"] = namespace
        if group_ids:
            payload["group_ids"] = list(group_ids)
        if timestamp_format:
            payload["timestamp_format"] = timestamp_format
        return self._request("POST", "", json=payload)

    # ── 2. poll job ──────────────────────────────────────────────────────
    def get_job(self, job_id: str) -> Any:
        """GET /v1/memories/jobs/{job_id} — poll an async ingest job."""
        return self._request("GET", f"/jobs/{job_id}")

    # ── 3. search ────────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        group_ids: Sequence[str] | None = None,
        mode: Mode = "compose",
    ) -> Any:
        """POST /v1/memories/search — agentic memory search.

        At least one scope axis (user_id / agent_id / app_id / group_ids) is
        required by the server. ``compose`` also returns a prompt-ready
        ``context`` block; ``retrieve`` returns ranked rows only (no LLM).
        """
        payload: dict[str, Any] = {"query": query, "mode": mode}
        _apply_scope(payload, user_id, agent_id, app_id, group_ids)
        return self._request("POST", "/search", json=payload)

    # ── 4. trigger / directive recall ────────────────────────────────────
    def trigger(
        self,
        *,
        tool: str | None = None,
        args: dict | None = None,
        output: str | None = None,
        entities: Sequence[str] | None = None,
        task: str | None = None,
        namespace: str | None = None,
        include: Iterable[MemoryType] | None = None,
        mode: Mode = "compose",
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        group_ids: Sequence[str] | None = None,
    ) -> Any:
        """POST /v1/memories/trigger — symbol-tripwire procedural recall.

        Fire on either an ``action`` (the in-flight tool call — ``tool`` /
        ``args`` / ``output``) or a pre-extracted ``entities`` list. ``include``
        defaults server-side to both ``lesson`` and ``procedure``.
        """
        payload: dict[str, Any] = {"mode": mode}
        action = {k: v for k, v in (("tool", tool), ("args", args), ("output", output)) if v}
        if action:
            payload["action"] = action
        if entities:
            payload["entities"] = list(entities)
        if task:
            payload["task"] = task
        if namespace:
            payload["namespace"] = namespace
        if include:
            payload["include"] = list(include)
        _apply_scope(payload, user_id, agent_id, app_id, group_ids)
        return self._request("POST", "/trigger", json=payload)

    # ── 5. list ──────────────────────────────────────────────────────────
    def list_memories(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        app_id: str | None = None,
        group_ids: Sequence[str] | None = None,
        type: MemoryType | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Any:
        """GET /v1/memories/ — list memories for a scope."""
        params: dict[str, Any] = {}
        if user_id:
            params["user_id"] = user_id
        if agent_id:
            params["agent_id"] = agent_id
        if app_id:
            params["app_id"] = app_id
        if group_ids:
            params["group_ids"] = list(group_ids)
        if type:
            params["type"] = type
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "", params=params)

    # ── 6. get one ───────────────────────────────────────────────────────
    def get_memory(self, memory_id: str) -> Any:
        """GET /v1/memories/{id}."""
        return self._request("GET", f"/{memory_id}")

    # ── 7. revision chain ────────────────────────────────────────────────
    def get_revisions(self, memory_id: str) -> Any:
        """GET /v1/memories/{id}/revisions — the supersede chain."""
        return self._request("GET", f"/{memory_id}/revisions")

    # ── 8. delete / retract ──────────────────────────────────────────────
    def delete_memory(self, memory_id: str) -> Any:
        """DELETE /v1/memories/{id} — retract a memory."""
        return self._request("DELETE", f"/{memory_id}")


def _apply_scope(
    payload: dict[str, Any],
    user_id: str | None,
    agent_id: str | None,
    app_id: str | None,
    group_ids: Sequence[str] | None,
) -> None:
    """Attach the scope axes a caller supplied. Omitted axes stay unconstrained;
    the server rejects a fully unscoped read."""
    if user_id:
        payload["user_id"] = user_id
    if agent_id:
        payload["agent_id"] = agent_id
    if app_id:
        payload["app_id"] = app_id
    if group_ids:
        payload["group_ids"] = list(group_ids)
