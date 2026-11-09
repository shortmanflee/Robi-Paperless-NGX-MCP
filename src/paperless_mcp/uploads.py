"""Single-use upload and download links for files too large to pass through a tool call.

Flow: a tool creates a link (an unguessable 256-bit token in the URL path). The client
streams the file with `curl -T file <url>`, or downloads with `curl -o file <url>`. The
token is the credential, so these routes sit outside the MCP bearer auth. Links expire,
work once, and never hold the whole file in memory.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.ingest import DocumentMeta, detect_mime, submit_document

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

log = logging.getLogger(__name__)

SlotKind = Literal["upload", "download"]
SlotState = Literal["waiting", "receiving", "processing", "done", "failed"]
RESULT_RETENTION = 3600  # keep finished slot results this long for get_upload_status


@dataclass
class Slot:
    kind: SlotKind
    token: str
    id: str
    expires_at: float
    filename: str
    meta: DocumentMeta | None = None
    wait_seconds: float = 0
    document_id: int | None = None
    original: bool = False
    state: SlotState = "waiting"
    result: dict[str, Any] | None = None
    finished_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "slot_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "filename": self.filename,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.expires_at)),
        }
        if self.result is not None:
            out["result"] = self.result
        return out


class UploadStore:
    """In-memory registry of upload/download links plus the staging directory."""

    def __init__(self, directory: Path, *, ttl: int, max_bytes: int, max_concurrent: int):
        self.dir = directory
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._by_token: dict[str, Slot] = {}
        self._by_id: dict[str, Slot] = {}
        self._active = 0
        self._max_concurrent = max_concurrent

    # -- staging files -----------------------------------------------------------
    def prepare(self) -> None:
        """Create the staging dir and remove files orphaned by a previous run."""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for stale in self.dir.glob("*.part"):
            stale.unlink(missing_ok=True)

    def temp_path(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.dir / f"{uuid.uuid4().hex}.part"

    # -- concurrency ------------------------------------------------------------------
    def try_acquire(self) -> bool:
        if self._active >= self._max_concurrent:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        self._active = max(0, self._active - 1)

    # -- slots ---------------------------------------------------------------------
    def sweep(self) -> None:
        now = time.time()
        for slot in list(self._by_id.values()):
            expired_unused = slot.state == "waiting" and slot.expires_at < now
            done_at = slot.finished_at
            stale_result = done_at is not None and done_at + RESULT_RETENTION < now
            if expired_unused or stale_result:
                self._by_id.pop(slot.id, None)
                self._by_token.pop(slot.token, None)

    def _add(self, slot: Slot) -> Slot:
        self.sweep()
        self._by_token[slot.token] = slot
        self._by_id[slot.id] = slot
        return slot

    def create_upload(self, filename: str, meta: DocumentMeta, wait_seconds: float) -> Slot:
        return self._add(
            Slot(
                kind="upload",
                token=secrets.token_urlsafe(32),
                id=f"up_{secrets.token_hex(6)}",
                expires_at=time.time() + self.ttl,
                filename=filename,
                meta=meta,
                wait_seconds=wait_seconds,
            )
        )

    def create_download(self, document_id: int, filename: str, original: bool) -> Slot:
        return self._add(
            Slot(
                kind="download",
                token=secrets.token_urlsafe(32),
                id=f"dl_{secrets.token_hex(6)}",
                expires_at=time.time() + self.ttl,
                filename=filename,
                document_id=document_id,
                original=original,
            )
        )

    def claim(self, token: str, kind: SlotKind) -> Slot | None:
        """Atomically move a waiting, unexpired slot to 'receiving' (single use)."""
        self.sweep()
        slot = self._by_token.get(token)
        if slot is None or slot.kind != kind or slot.state != "waiting":
            return None
        if slot.expires_at < time.time():
            return None
        slot.state = "receiving"
        return slot

    def unclaim(self, slot: Slot) -> None:
        """Hand a slot back (request rejected before any data was accepted)."""
        if slot.state == "receiving":
            slot.state = "waiting"

    def finish(self, slot: Slot, state: SlotState, result: dict[str, Any]) -> None:
        slot.state = state
        slot.result = result
        slot.finished_at = time.time()

    def get(self, slot_id: str) -> Slot | None:
        self.sweep()
        return self._by_id.get(slot_id)


def link_base(settings: Settings) -> str:
    """Base URL for links: MCP_PUBLIC_URL, else derived from the current MCP request."""
    if settings.mcp_public_url:
        return settings.mcp_public_url
    try:
        from fastmcp.server.dependencies import get_http_request

        return str(get_http_request().base_url).rstrip("/")
    except RuntimeError as exc:
        raise PaperlessError(
            500, "set MCP_PUBLIC_URL so the server can build upload/download links"
        ) from exc


def _json(status: int, **body: Any) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def register_routes(
    mcp: FastMCP, pc: PaperlessClient, store: UploadStore, settings: Settings
) -> None:
    max_bytes = store.max_bytes
    limit_mb = f"{max_bytes / 1024 / 1024:.0f} MB"

    if not settings.mcp_read_only:

        @mcp.custom_route("/uploads/{token}", methods=["PUT"], include_in_schema=False)
        async def receive_upload(request: Request) -> Response:
            token = request.path_params["token"]
            slot = store.claim(token, "upload")
            if slot is None:
                return _json(404, error="unknown, expired or already used upload link")
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                store.unclaim(slot)
                return _json(413, error=f"file is larger than the {limit_mb} limit")
            if not store.try_acquire():
                store.unclaim(slot)
                return _json(429, error="too many uploads in progress; retry shortly")
            path = store.temp_path()
            try:
                written = 0
                with path.open("wb") as fh:
                    async for chunk in request.stream():
                        written += len(chunk)
                        if written > max_bytes:
                            raise PaperlessError(413, f"file is larger than the {limit_mb} limit")
                        fh.write(chunk)
                slot.state = "processing"
                mime = detect_mime(path, slot.filename)
                assert slot.meta is not None
                result = await submit_document(
                    pc, path, slot.filename, mime, slot.meta, wait_seconds=slot.wait_seconds
                )
                store.finish(slot, "done", result)
                return JSONResponse(result, status_code=200 if result.get("document_id") else 202)
            except PaperlessError as exc:
                status = exc.status if 400 <= exc.status < 600 else 502
                store.finish(slot, "failed", {"error": exc.message, "status": status})
                return _json(status, error=exc.message)
            except ClientDisconnect:
                store.finish(slot, "failed", {"error": "client disconnected mid-upload"})
                return _json(400, error="client disconnected mid-upload")
            except Exception:
                log.exception("upload %s failed", slot.id)
                store.finish(slot, "failed", {"error": "internal error"})
                return _json(500, error="internal error")
            finally:
                path.unlink(missing_ok=True)
                store.release()

    @mcp.custom_route("/downloads/{token}", methods=["GET"], include_in_schema=False)
    async def send_download(request: Request) -> Response:
        token = request.path_params["token"]
        slot = store.claim(token, "download")
        if slot is None:
            return _json(404, error="unknown, expired or already used download link")
        if not store.try_acquire():
            store.unclaim(slot)
            return _json(429, error="too many transfers in progress; retry shortly")
        stack = AsyncExitStack()
        try:
            upstream = await stack.enter_async_context(
                pc.stream(
                    "GET",
                    f"documents/{slot.document_id}/download",
                    params={"original": "true"} if slot.original else None,
                )
            )
        except PaperlessError as exc:
            await stack.aclose()
            store.release()
            store.finish(slot, "failed", {"error": exc.message})
            return _json(exc.status if 400 <= exc.status < 600 else 502, error=exc.message)

        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(slot.filename)}",
        }
        if length := upstream.headers.get("content-length"):
            headers["Content-Length"] = length
        media_type = upstream.headers.get("content-type", "application/octet-stream")

        async def body():
            sent = 0
            try:
                async for chunk in upstream.aiter_bytes(1024 * 256):
                    sent += len(chunk)
                    yield chunk
                store.finish(slot, "done", {"size_bytes": sent})
            except (asyncio.CancelledError, Exception) as exc:
                store.finish(slot, "failed", {"error": f"transfer interrupted: {exc!r}"})
                raise
            finally:
                await stack.aclose()
                store.release()

        return StreamingResponse(body(), media_type=media_type, headers=headers)
