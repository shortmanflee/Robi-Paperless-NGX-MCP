"""Async HTTP client for the Paperless-NGX REST API (v3.x, API version 10)."""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastmcp.exceptions import ToolError

log = logging.getLogger(__name__)

Expect = Literal["json", "bytes", "text", "none", "response"]

# Endpoints whose objects can be referenced by name as well as id.
LOOKUP_KINDS = ("tags", "correspondents", "document_types", "storage_paths", "custom_fields")
NAME_CACHE_SECONDS = 60


class PaperlessError(ToolError):
    """A Paperless API failure. Subclasses ToolError so the message reaches the MCP client."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        prefix = f"Paperless error {status}" if status else "Paperless unreachable"
        super().__init__(f"{prefix}: {message}")


def _flatten_errors(body: Any, prefix: str = "") -> list[str]:
    """Turn DRF error payloads ({"name": ["already exists"]}, nested lists...) into lines."""
    if isinstance(body, dict):
        out: list[str] = []
        for key, value in body.items():
            label = prefix if key in ("detail", "non_field_errors", "error") else key
            if prefix and label != prefix:
                label = f"{prefix}.{label}"
            out.extend(_flatten_errors(value, label))
        return out
    if isinstance(body, list):
        out = []
        for i, value in enumerate(body):
            sub = f"{prefix}[{i}]" if isinstance(value, dict | list) and prefix else prefix
            out.extend(_flatten_errors(value, sub))
        return out
    text = str(body)
    return [f"{prefix}: {text}" if prefix else text]


def error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        text = resp.text.strip()
        if text.lstrip().startswith("<"):
            return resp.reason_phrase or "error"
        return text[:300] or resp.reason_phrase
    lines = _flatten_errors(body)
    return "; ".join(lines)[:600] or resp.reason_phrase


def api_path(path: str) -> str:
    """Paperless (Django) wants a trailing slash on every endpoint."""
    path = "/" + path.lstrip("/")
    return path if path.endswith("/") else path + "/"


class PaperlessClient:
    """Thin authenticated wrapper around Paperless-NGX's /api endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        api_version: int = 10,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        upload_timeout: float = 600.0,
        public_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token and not (username and password):
            raise ValueError("PaperlessClient needs a token or username+password")
        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/api"
        self.public_url = (public_url or base_url).rstrip("/")
        if token:
            token = token.strip()
            if token.lower().startswith("token "):
                token = token[6:].strip()
            self._auth = f"Token {token}"
        else:
            basic = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._auth = f"Basic {basic}"
        self.api_version = api_version
        self._verify = verify_ssl
        self._timeout = timeout
        self.upload_timeout = upload_timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self.server_version: str | None = None
        self.server_api_version: str | None = None
        self._names: dict[str, tuple[float, dict[int, str]]] = {}

    # -- lifecycle -------------------------------------------------------------
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.api_base,
                verify=self._verify,
                timeout=self._timeout,
                transport=self._transport,
                headers={
                    "Accept": f"application/json; version={self.api_version}",
                    "Authorization": self._auth,
                    "User-Agent": "paperless-mcp",
                },
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    def _note_versions(self, resp: httpx.Response) -> None:
        if v := resp.headers.get("x-version"):
            self.server_version = v
        if v := resp.headers.get("x-api-version"):
            self.server_api_version = v

    # -- requests --------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        expect: Expect = "json",
        timeout: float | None = None,
    ) -> Any:
        if params:
            params = {k: v for k, v in params.items() if v is not None and v != []}
        kw: dict[str, Any] = {"params": params, "json": json, "data": data, "files": files}
        if timeout is not None:
            kw["timeout"] = timeout
        try:
            resp = await self.http.request(method, api_path(path), **kw)
        except httpx.HTTPError as exc:
            raise PaperlessError(0, f"{self.base_url}: {exc}") from exc
        self._note_versions(resp)
        if resp.status_code >= 400:
            raise PaperlessError(resp.status_code, error_message(resp))
        if expect == "response":
            return resp
        if expect == "none" or resp.status_code == 204 or not resp.content:
            return None
        if expect == "bytes":
            return resp.content
        if expect == "text":
            return resp.text
        return resp.json()

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> Any:
        return await self.request("PUT", path, **kw)

    async def patch(self, path: str, **kw: Any) -> Any:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, expect="none", **kw)

    @asynccontextmanager
    async def stream(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> AsyncIterator[httpx.Response]:
        """Stream a response body (large downloads). Raises PaperlessError on HTTP errors."""
        try:
            async with self.http.stream(
                method, api_path(path), params=params, timeout=self.upload_timeout
            ) as resp:
                self._note_versions(resp)
                if resp.status_code >= 400:
                    await resp.aread()
                    raise PaperlessError(resp.status_code, error_message(resp))
                yield resp
        except httpx.HTTPError as exc:
            raise PaperlessError(0, f"{self.base_url}: {exc}") from exc

    # -- pagination ----------------------------------------------------------------
    async def page(
        self, path: str, params: dict[str, Any] | None = None, *, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        """Fetch one page of a DRF list endpoint."""
        body = await self.get(path, params={**(params or {}), "page": page, "page_size": page_size})
        if isinstance(body, list):  # unpaginated endpoint
            return {"count": len(body), "results": body}
        return body or {"count": 0, "results": []}

    async def paginate(
        self, path: str, params: dict[str, Any] | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch every result of a list endpoint (up to `limit`)."""
        out: list[dict[str, Any]] = []
        page_no = 1
        while True:
            body = await self.page(path, params, page=page_no, page_size=100)
            out.extend(body.get("results") or [])
            if limit is not None and len(out) >= limit:
                return out[:limit]
            if not body.get("next"):
                return out
            page_no += 1

    # -- name/id resolution ------------------------------------------------------------
    async def resolve_id(self, kind: str, value: int | str) -> int:
        """Accept an id (int or numeric string) or an exact (case-insensitive) name."""
        if kind not in LOOKUP_KINDS:
            raise ValueError(f"unknown lookup kind {kind}")
        if isinstance(value, bool):
            raise PaperlessError(400, f"invalid {kind} reference {value!r}")
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        body = await self.get(kind, params={"name__iexact": text, "page_size": 2})
        results = body.get("results") if isinstance(body, dict) else body
        if not results:
            singular = kind.rstrip("s").replace("_", " ")
            raise PaperlessError(404, f"no {singular} named {text!r}")
        return int(results[0]["id"])

    async def resolve_ids(self, kind: str, values: list[int | str] | None) -> list[int]:
        return [await self.resolve_id(kind, v) for v in values or []]

    async def resolve_optional(self, kind: str, value: int | str | None) -> int | None:
        """None stays None (leave unchanged); '' or 0 means 'clear' and is returned as 0."""
        if value is None:
            return None
        if value in ("", 0):
            return 0
        return await self.resolve_id(kind, value)

    # -- id -> name cache (for readable document summaries) -------------------------------
    async def names(self, kind: str) -> dict[int, str]:
        cached = self._names.get(kind)
        if cached and time.monotonic() - cached[0] < NAME_CACHE_SECONDS:
            return cached[1]
        rows = await self.paginate(kind, {"fields": "id,name"})
        mapping = {int(r["id"]): str(r.get("name")) for r in rows if r.get("id") is not None}
        self._names[kind] = (time.monotonic(), mapping)
        return mapping

    def invalidate(self, kind: str | None = None) -> None:
        if kind is None:
            self._names.clear()
        else:
            self._names.pop(kind, None)
