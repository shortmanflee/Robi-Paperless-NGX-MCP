"""Shared ingest pipeline: file on disk -> Paperless post_document -> (optionally) wait for task.

Every upload path (base64, upload link, URL fetch, local path) ends here, so type checks,
metadata resolution and task polling behave identically everywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from paperless_mcp.client import PaperlessClient, PaperlessError

log = logging.getLogger(__name__)

# Paperless-supported types by parser (tesseract, text, tika, mail). Office formats need Tika.
SUPPORTED_MIME = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/heic",
    "text/plain",
    "text/csv",
    "application/csv",
    "text/rtf",
    "text/html",
    "message/rfc822",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.graphics",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
}
_EXTRA_TYPES = {".eml": "message/rfc822", ".heic": "image/heic", ".rtf": "text/rtf"}

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

POLL_INTERVAL = 1.0


class UnsupportedFile(PaperlessError):
    def __init__(self, message: str):
        super().__init__(415, message)


def guess_mime(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    return _EXTRA_TYPES.get(ext) or mimetypes.guess_type(filename)[0]


def _looks_like_html(head: bytes) -> bool:
    sample = head[:512].lstrip().lower()
    return sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def detect_mime(path: Path, filename: str, declared: str | None = None) -> str:
    """Work out the file type from its magic bytes, falling back to the name/declared type.

    Raises UnsupportedFile for empty files and types Paperless can't consume.
    """
    with path.open("rb") as fh:
        head = fh.read(4096)
    if not head:
        raise UnsupportedFile("the file is empty")
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM" and head[6:10] == b"\x00\x00\x00\x00":  # BMP: reserved bytes are 0
        return "image/bmp"
    by_name = guess_mime(filename)
    if head.startswith((_ZIP, _OLE)):
        # Office containers: trust the name, since the magic is shared with plain ZIP/OLE files.
        if by_name in SUPPORTED_MIME and by_name.startswith("application/"):
            return by_name
        raise UnsupportedFile(
            f"{filename!r} is a ZIP/OLE container but not a recognised office document"
        )
    if b"\x00" in head:
        raise UnsupportedFile(
            f"{filename!r} is a binary format Paperless can't consume "
            f"(declared {declared or by_name or 'unknown'})"
        )
    if _looks_like_html(head):
        return "text/html"
    for candidate in (declared, by_name):
        if candidate in SUPPORTED_MIME:
            return candidate  # type: ignore[return-value]
    return "text/plain"


class DocumentMeta(BaseModel):
    """Metadata applied when Paperless consumes the file. Names or ids are accepted."""

    title: str | None = None
    created: str | None = Field(default=None, description="YYYY-MM-DD or ISO datetime")
    correspondent: int | str | None = None
    document_type: int | str | None = None
    storage_path: int | str | None = None
    tags: list[int | str] = Field(default_factory=list)
    archive_serial_number: int | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


async def custom_field_values(pc: PaperlessClient, values: dict[str, Any]) -> dict[int, Any]:
    """Map {field name or id: value} to {field id: value}.

    Select fields accept the option label as well as the option id.
    """
    out: dict[int, Any] = {}
    for key, value in values.items():
        field_id = await pc.resolve_id("custom_fields", key)
        if isinstance(value, str) and value:
            field = await pc.get(f"custom_fields/{field_id}")
            if field.get("data_type") == "select":
                options = (field.get("extra_data") or {}).get("select_options") or []
                for opt in options:
                    if value in (opt.get("id"), opt.get("label")) or (
                        str(opt.get("label", "")).lower() == value.lower()
                    ):
                        value = opt.get("id")
                        break
                else:
                    labels = [o.get("label") for o in options]
                    raise PaperlessError(
                        400, f"{value!r} is not an option of {field.get('name')!r}: {labels}"
                    )
        out[field_id] = value
    return out


async def meta_form(pc: PaperlessClient, meta: DocumentMeta) -> dict[str, Any]:
    """Build post_document form fields from metadata, resolving names to ids."""
    form: dict[str, Any] = {}
    if meta.title:
        form["title"] = meta.title
    if meta.created:
        form["created"] = meta.created
    for kind, value in (
        ("correspondents", meta.correspondent),
        ("document_types", meta.document_type),
        ("storage_paths", meta.storage_path),
    ):
        if value not in (None, ""):
            form[kind.removesuffix("s")] = str(await pc.resolve_id(kind, value))
    if meta.tags:
        form["tags"] = [str(i) for i in await pc.resolve_ids("tags", meta.tags)]
    if meta.archive_serial_number is not None:
        form["archive_serial_number"] = str(meta.archive_serial_number)
    if meta.custom_fields:
        values = await custom_field_values(pc, meta.custom_fields)
        form["custom_fields"] = json.dumps({str(k): v for k, v in values.items()})
    return form


def summarize_task(task: dict[str, Any]) -> dict[str, Any]:
    result = task.get("result_data") or {}
    out = {
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "task_type": task.get("task_type"),
        "document_id": result.get("document_id"),
        "duplicate_of": result.get("duplicate_of"),
        "error": result.get("error_message") or result.get("reason"),
        "file": (task.get("input_data") or {}).get("filename"),
        "created": task.get("date_created"),
        "done": task.get("date_done"),
        "acknowledged": task.get("acknowledged"),
        "id": task.get("id"),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


async def get_task(pc: PaperlessClient, task_id: str) -> dict[str, Any] | None:
    body = await pc.get("tasks", params={"task_id": task_id})
    results = body.get("results") if isinstance(body, dict) else body
    return results[0] if results else None


async def wait_for_task(pc: PaperlessClient, task_id: str, wait_seconds: float) -> dict[str, Any]:
    """Poll a consume task until it finishes or `wait_seconds` pass."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait_seconds)
    while True:
        task = await get_task(pc, task_id)
        if task and task.get("status") in ("success", "failure", "revoked"):
            return summarize_task(task)
        if loop.time() >= deadline:
            out = summarize_task(task) if task else {"task_id": task_id}
            out["status"] = (task or {}).get("status") or "pending"
            out["hint"] = "Paperless is still processing; check again with get_task(task_id)."
            return out
        await asyncio.sleep(min(POLL_INTERVAL, max(0.05, deadline - loop.time())))


async def submit_document(
    pc: PaperlessClient,
    path: Path,
    filename: str,
    mime: str,
    meta: DocumentMeta,
    *,
    wait_seconds: float = 0,
) -> dict[str, Any]:
    """Stream a file from disk to Paperless and return the consume task status."""
    form = await meta_form(pc, meta)
    size = path.stat().st_size
    with path.open("rb") as fh:
        task_id = await pc.post(
            "documents/post_document",
            data=form,
            files={"document": (filename, fh, mime)},
            timeout=pc.upload_timeout,
        )
    if not isinstance(task_id, str):
        raise PaperlessError(502, f"unexpected post_document response: {task_id!r}")
    log.info("Submitted %s (%d bytes) to Paperless as task %s", filename, size, task_id)
    base = {"task_id": task_id, "status": "pending", "filename": filename, "size_bytes": size}
    if wait_seconds <= 0:
        return base
    return base | await wait_for_task(pc, task_id, wait_seconds)
