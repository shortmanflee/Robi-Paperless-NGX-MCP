"""FastMCP application factory."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.transforms.search import BM25SearchTransform, RegexSearchTransform
from starlette.requests import Request
from starlette.responses import JSONResponse

from paperless_mcp import __version__
from paperless_mcp.auth import StaticBearerVerifier
from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.config import Settings
from paperless_mcp.tools import register_all
from paperless_mcp.uploads import UploadStore, register_routes

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Tools for a Paperless-NGX document management server.

Find documents with search_documents (full-text `text` plus filters), then get_document
for details and OCR text. Tags, correspondents, document types, storage paths and custom
fields can be referenced by name or id everywhere.

Adding documents:
- A file on the user's machine, any size: create_upload_link, then run the returned curl
  command in a shell. Prefer this for anything over a few MB (e.g. PDFs up to 100 MB).
- A file on the web: upload_document_from_url with the direct link to the file.
- Small content you already have: upload_document (base64).
Paperless consumes uploads in the background; uploads wait briefly and return the new
document_id, or a task_id to check with get_task.

Large downloads: create_download_link gives a curl command. Deleting documents moves them
to the trash (restore_documents undoes it); empty_trash and PDF edits are permanent -
confirm with the user first.
"""

HEALTH_CACHE_SECONDS = 30

# Tools that stay listed when server-side tool search is enabled.
ALWAYS_VISIBLE = ("search_documents", "get_document")


def build_client(settings: Settings) -> PaperlessClient:
    return PaperlessClient(
        settings.paperless_url,
        token=settings.paperless_token.get_secret_value() if settings.paperless_token else None,
        username=settings.paperless_username,
        password=settings.paperless_password.get_secret_value()
        if settings.paperless_password
        else None,
        api_version=settings.paperless_api_version,
        verify_ssl=settings.paperless_verify_ssl,
        timeout=settings.paperless_timeout,
        upload_timeout=settings.paperless_upload_timeout,
        public_url=settings.paperless_public_url,
    )


def build_store(settings: Settings) -> UploadStore:
    return UploadStore(
        settings.mcp_upload_dir,
        ttl=settings.mcp_upload_slot_ttl,
        max_bytes=settings.max_upload_bytes,
        max_concurrent=settings.mcp_max_concurrent_uploads,
    )


def create_server(
    settings: Settings, pc: PaperlessClient | None = None, store: UploadStore | None = None
) -> FastMCP:
    pc = pc or build_client(settings)
    store = store or build_store(settings)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
        store.prepare()
        try:
            yield {"paperless": pc}
        finally:
            await pc.aclose()

    auth = StaticBearerVerifier(settings.auth_tokens) if settings.mcp_transport == "http" else None
    mcp = FastMCP(
        name="Paperless-NGX",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        lifespan=lifespan,
        mask_error_details=True,
    )
    register_all(mcp, pc, settings, store)
    if settings.mcp_transport == "http":
        register_routes(mcp, pc, store, settings)
    if settings.mcp_tool_search != "off":
        transform_cls = (
            BM25SearchTransform if settings.mcp_tool_search == "bm25" else RegexSearchTransform
        )
        mcp.add_transform(
            transform_cls(
                max_results=settings.mcp_tool_search_max_results,
                always_visible=list(ALWAYS_VISIBLE),
            )
        )

    def _named(kind: str):
        async def resource() -> list[dict[str, Any]]:
            return [{"id": i, "name": n} for i, n in (await pc.names(kind)).items()]

        return resource

    for kind, label in (
        ("tags", "All tags"),
        ("correspondents", "All correspondents"),
        ("document_types", "All document types"),
        ("storage_paths", "All storage paths"),
        ("custom_fields", "All custom fields"),
    ):
        mcp.resource(
            f"paperless://{kind.replace('_', '-')}",
            name=kind,
            description=f"{label} (ids and names).",
            mime_type="application/json",
        )(_named(kind))

    health_cache: dict[str, Any] = {"at": 0.0, "ok": False, "detail": None}

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> JSONResponse:
        """Unauthenticated liveness probe. Reports Paperless reachability (cached)."""
        now = time.monotonic()
        if now - health_cache["at"] > HEALTH_CACHE_SECONDS:
            try:
                await pc.get("ui_settings")
                health_cache.update(ok=True, detail=None)
            except PaperlessError as exc:
                health_cache.update(ok=False, detail=exc.message)
            health_cache["at"] = now
        body = {"status": "ok", "paperless": "ok" if health_cache["ok"] else "unreachable"}
        if health_cache["detail"]:
            body["detail"] = health_cache["detail"]
        # MCP itself is alive either way; Paperless being down shouldn't restart this container.
        return JSONResponse(body)

    return mcp
