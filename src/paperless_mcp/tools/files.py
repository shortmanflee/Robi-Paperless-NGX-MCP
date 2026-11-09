"""Getting files into (and large files out of) Paperless.

All upload paths stream through `ingest.submit_document`:
  - upload_document            small files as base64 (<= MCP_MAX_INLINE_MB)
  - create_upload_link         large local files: client runs `curl -T file <url>`
  - upload_document_from_url   server downloads a public HTTPS URL (SSRF-guarded)
  - upload_document_from_path  stdio only: read a file on this machine directly
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from paperless_mcp import fetch
from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.ingest import (
    DocumentMeta,
    detect_mime,
    guess_mime,
    meta_form,
    submit_document,
)
from paperless_mcp.tools._common import READ, WRITE, CustomFieldValues, Date, DocId, RefArg, RefList
from paperless_mcp.uploads import UploadStore, link_base

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

Title = Annotated[str | None, Field(description="Title (default: Paperless derives it)")]
Asn = Annotated[int | None, Field(description="Archive serial number")]
Wait = Annotated[
    float,
    Field(
        ge=0,
        le=300,
        description="Seconds to wait for Paperless to finish consuming (0 = return at once "
        "with the task id; check later with get_task)",
    ),
]


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _meta(
    title: str | None,
    created: str | None,
    correspondent: Any,
    document_type: Any,
    storage_path: Any,
    tags: list[Any] | None,
    archive_serial_number: int | None,
    custom_fields: dict[str, Any] | None,
) -> DocumentMeta:
    return DocumentMeta(
        title=title,
        created=created,
        correspondent=correspondent,
        document_type=document_type,
        storage_path=storage_path,
        tags=tags or [],
        archive_serial_number=archive_serial_number,
        custom_fields=custom_fields or {},
    )


def with_extension(filename: str, mime: str) -> str:
    """Make sure the name's extension matches the detected type (helps Paperless and humans)."""
    if guess_mime(filename) == mime:
        return filename
    ext = mimetypes.guess_extension(mime) or ""
    return f"{filename}{ext}" if ext and not filename.lower().endswith(ext) else filename


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip() or "document"
    return name[:200]


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings, store: UploadStore) -> None:
    max_bytes = store.max_bytes  # one source of truth with the upload route
    max_inline = settings.max_inline_bytes
    http = settings.mcp_transport == "http"

    if http:

        @mcp.tool(annotations=READ)
        async def create_download_link(
            document_id: DocId,
            original: Annotated[
                bool, Field(description="Original upload instead of the archived PDF")
            ] = False,
        ) -> dict[str, Any]:
            """Create a single-use, short-lived URL to download a document of any size with
            curl (the file streams from Paperless and never passes through this chat)."""
            doc = await pc.get(
                f"documents/{document_id}",
                params={"fields": "id,title,original_file_name,archived_file_name"},
            )
            name = (
                doc.get("original_file_name")
                if original
                else doc.get("archived_file_name") or doc.get("original_file_name")
            ) or f"document-{document_id}.pdf"
            slot = store.create_download(document_id, _safe_name(name), original)
            url = f"{link_base(settings)}/downloads/{slot.token}"
            return {
                "download_url": url,
                "curl_command": f"curl -fL -o {shlex.quote(slot.filename)} {shlex.quote(url)}",
                "slot_id": slot.id,
                "expires_at": slot.public()["expires_at"],
                "note": "Works once. Run the curl command where the file should be saved.",
            }

        @mcp.tool(annotations=READ)
        async def get_link_status(slot_id: str) -> dict[str, Any]:
            """Status of an upload or download link (waiting, receiving, processing, done or
            failed), including the Paperless task result for uploads."""
            slot = store.get(slot_id)
            if slot is None:
                raise PaperlessError(404, f"no link {slot_id} (unknown or expired)")
            return slot.public()

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE)
    async def upload_document(
        filename: Annotated[str, Field(description="File name including extension")],
        content_base64: Annotated[str, Field(description="File contents, base64-encoded")],
        title: Title = None,
        created: Date = None,
        correspondent: RefArg = None,
        document_type: RefArg = None,
        storage_path: RefArg = None,
        tags: RefList = None,
        archive_serial_number: Asn = None,
        custom_fields: CustomFieldValues = None,
        wait_seconds: Wait = 30,
    ) -> dict[str, Any]:
        """Upload a SMALL file (base64, up to MCP_MAX_INLINE_MB) to Paperless. For large
        files use create_upload_link (local file) or upload_document_from_url (web)."""
        try:
            payload = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PaperlessError(400, f"content_base64 is not valid base64: {exc}") from exc
        if len(payload) > max_inline:
            raise PaperlessError(
                413,
                f"file is {_mb(len(payload))}; inline limit is {_mb(max_inline)}. "
                "Use create_upload_link instead.",
            )
        name = _safe_name(filename)
        path = store.temp_path()
        try:
            path.write_bytes(payload)
            del payload
            mime = detect_mime(path, name)
            meta = _meta(
                title, created, correspondent, document_type, storage_path, tags,
                archive_serial_number, custom_fields,
            )  # fmt: skip
            return await submit_document(
                pc, path, with_extension(name, mime), mime, meta, wait_seconds=wait_seconds
            )
        finally:
            path.unlink(missing_ok=True)

    if http:

        @mcp.tool(annotations=WRITE)
        async def create_upload_link(
            filename: Annotated[str, Field(description="File name including extension")],
            title: Title = None,
            created: Date = None,
            correspondent: RefArg = None,
            document_type: RefArg = None,
            storage_path: RefArg = None,
            tags: RefList = None,
            archive_serial_number: Asn = None,
            custom_fields: CustomFieldValues = None,
            wait_seconds: Wait = 30,
        ) -> dict[str, Any]:
            """Upload a LARGE local file (up to MCP_MAX_UPLOAD_MB, e.g. a 100 MB PDF). Returns
            a single-use URL and a curl command: run it in a shell where the file is. The file
            streams straight to Paperless, and curl prints the result (task id and, once
            consumed, the document id). Metadata given here is applied to the document."""
            # Resolve names now so typos fail before the user uploads 100 MB.
            meta = _meta(
                title, created, correspondent, document_type, storage_path, tags,
                archive_serial_number, custom_fields,
            )  # fmt: skip
            await meta_form(pc, meta)
            name = _safe_name(filename)
            slot = store.create_upload(name, meta, wait_seconds)
            url = f"{link_base(settings)}/uploads/{slot.token}"
            return {
                "upload_url": url,
                "curl_command": f"curl -fsS -T {shlex.quote(filename)} {shlex.quote(url)}",
                "method": "PUT (raw file body)",
                "slot_id": slot.id,
                "max_size": _mb(max_bytes),
                "expires_at": slot.public()["expires_at"],
                "note": "Works once. Use get_link_status(slot_id) if the curl output is lost.",
            }

    if settings.mcp_allow_url_fetch:

        @mcp.tool(annotations=WRITE.model_copy(update={"open_world_hint": True}))
        async def upload_document_from_url(
            url: Annotated[
                str, Field(description="Public https:// URL that serves the file itself")
            ],
            filename: Annotated[
                str | None, Field(description="Override the file name (default: from server)")
            ] = None,
            title: Title = None,
            created: Date = None,
            correspondent: RefArg = None,
            document_type: RefArg = None,
            storage_path: RefArg = None,
            tags: RefList = None,
            archive_serial_number: Asn = None,
            custom_fields: CustomFieldValues = None,
            wait_seconds: Wait = 30,
        ) -> dict[str, Any]:
            """Download a document (e.g. a PDF up to MCP_MAX_UPLOAD_MB) from a public HTTPS URL
            and add it to Paperless. Private/LAN addresses are refused. The URL must point at
            the file, not a web page about it."""
            if not store.try_acquire():
                raise PaperlessError(429, "too many uploads in progress; retry shortly")
            path = store.temp_path()
            try:
                try:
                    res = await fetch.fetch_to_file(
                        url, path, max_bytes=max_bytes, timeout=settings.mcp_url_fetch_timeout
                    )
                except fetch.FetchError as exc:
                    raise PaperlessError(exc.status, f"download failed: {exc}") from exc
                name = _safe_name(filename or res.filename)
                mime = detect_mime(path, name, res.mime)
                if mime == "text/html":
                    raise PaperlessError(
                        415,
                        "the URL returned an HTML web page, not a document. "
                        "Find the direct link to the file (e.g. the .pdf link) and retry.",
                    )
                meta = _meta(
                    title, created, correspondent, document_type, storage_path, tags,
                    archive_serial_number, custom_fields,
                )  # fmt: skip
                result = await submit_document(
                    pc, path, with_extension(name, mime), mime, meta, wait_seconds=wait_seconds
                )
                return result | {"source_url": res.final_url}
            finally:
                path.unlink(missing_ok=True)
                store.release()

    if not http:
        roots = settings.local_upload_roots

        @mcp.tool(annotations=WRITE)
        async def upload_document_from_path(
            path: Annotated[str, Field(description="Path to a file on this computer")],
            title: Title = None,
            created: Date = None,
            correspondent: RefArg = None,
            document_type: RefArg = None,
            storage_path: RefArg = None,
            tags: RefList = None,
            archive_serial_number: Asn = None,
            custom_fields: CustomFieldValues = None,
            wait_seconds: Wait = 30,
        ) -> dict[str, Any]:
            """Upload a local file (up to MCP_MAX_UPLOAD_MB) by path. Only available when the
            server runs locally over stdio."""
            p = Path(path).expanduser().resolve()
            if roots and not any(p.is_relative_to(r) for r in roots):
                raise PaperlessError(
                    403, f"{p} is outside the allowed folders (MCP_LOCAL_UPLOAD_ROOTS)"
                )
            if not p.is_file():
                raise PaperlessError(404, f"{p} is not a file")
            size = p.stat().st_size
            if size > max_bytes:
                raise PaperlessError(413, f"file is {_mb(size)}; the limit is {_mb(max_bytes)}")
            mime = detect_mime(p, p.name)
            meta = _meta(
                title, created, correspondent, document_type, storage_path, tags,
                archive_serial_number, custom_fields,
            )  # fmt: skip
            return await submit_document(pc, p, p.name, mime, meta, wait_seconds=wait_seconds)
