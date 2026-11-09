"""Document tools: search, read, update, delete and the trash."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp.utilities.types import File, Image
from pydantic import Field

from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.ingest import custom_field_values
from paperless_mcp.tools._common import (
    DELETE,
    DOC_KINDS,
    READ,
    UPDATE,
    ClearableRef,
    CustomFieldValues,
    Date,
    DocId,
    Names,
    Page,
    PageSize,
    RefArg,
    RefList,
    document_detail,
    drop_empty,
    page_result,
    summarize_document,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

LIST_FIELDS = (
    "id,title,created,created_date,correspondent,document_type,storage_path,tags,"
    "archive_serial_number,page_count"
)
Ordering = Literal[
    "relevance",
    "created",
    "-created",
    "added",
    "-added",
    "modified",
    "-modified",
    "title",
    "-title",
    "archive_serial_number",
    "-archive_serial_number",
]


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


async def _ids_csv(pc: PaperlessClient, kind: str, values: list[Any] | None) -> str | None:
    if not values:
        return None
    return ",".join(str(i) for i in await pc.resolve_ids(kind, values))


async def summaries(pc: PaperlessClient, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = await Names(pc).load("tags", "correspondents", "document_types", "storage_paths")
    return [summarize_document(d, names) for d in docs]


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    max_inline = settings.max_inline_bytes

    # ------------------------------------------------------------------ read
    @mcp.tool(annotations=READ)
    async def search_documents(
        text: Annotated[
            str | None, Field(description="Full-text search over title, content and notes")
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Advanced Paperless query syntax, e.g. "
                "'tag:invoice correspondent:acme created:[2024 to 2025]'"
            ),
        ] = None,
        title_contains: str | None = None,
        content_contains: str | None = None,
        tags: Annotated[RefList, Field(description="Documents must have ALL of these")] = None,
        any_tags: Annotated[RefList, Field(description="Documents must have ANY of these")] = None,
        exclude_tags: Annotated[RefList, Field(description="Documents must have NONE")] = None,
        correspondent: RefArg = None,
        document_type: RefArg = None,
        storage_path: RefArg = None,
        created_from: Date = None,
        created_to: Date = None,
        added_from: Date = None,
        added_to: Date = None,
        archive_serial_number: int | None = None,
        in_inbox: Annotated[
            bool | None, Field(description="Only documents with an inbox tag")
        ] = None,
        custom_field_query: Annotated[
            str | None,
            Field(
                description="Paperless custom-field query as JSON, e.g. "
                '\'["Amount", "gt", 100]\' or \'["AND", [["Paid", "exact", true], ...]]\''
            ),
        ] = None,
        ordering: Annotated[
            Ordering | None,
            Field(description="Default: relevance when searching text, else newest first"),
        ] = None,
        page: Page = 1,
        page_size: PageSize = 25,
    ) -> dict[str, Any]:
        """Find documents by full-text search and/or filters. Returns compact summaries
        (use get_document for content and details). Tags, correspondents etc. accept
        names or ids. Combine `text` with filters to narrow results."""
        params: dict[str, Any] = {
            "text": text,
            "query": query,
            "title__icontains": title_contains,
            "content__icontains": content_contains,
            "tags__id__all": await _ids_csv(pc, "tags", tags),
            "tags__id__in": await _ids_csv(pc, "tags", any_tags),
            "tags__id__none": await _ids_csv(pc, "tags", exclude_tags),
            "created__date__gte": created_from,
            "created__date__lte": created_to,
            "added__date__gte": added_from,
            "added__date__lte": added_to,
            "archive_serial_number": archive_serial_number,
            "is_in_inbox": None if in_inbox is None else str(in_inbox).lower(),
            "custom_field_query": custom_field_query,
            "fields": LIST_FIELDS,
            "truncate_content": "true",
        }
        for kind, key, value in (
            ("correspondents", "correspondent__id", correspondent),
            ("document_types", "document_type__id", document_type),
            ("storage_paths", "storage_path__id", storage_path),
        ):
            if value not in (None, ""):
                params[key] = await pc.resolve_id(kind, value)
        searching = bool(text or query)
        ordering = ordering or ("relevance" if searching else "-created")
        if ordering != "relevance":
            params["ordering"] = ordering
        elif not searching:
            params["ordering"] = "-created"
        body = await pc.page("documents", params, page=page, page_size=page_size)
        return page_result(body, page, await summaries(pc, body.get("results") or []))

    @mcp.tool(annotations=READ)
    async def get_document(
        document_id: DocId,
        include_content: Annotated[
            bool, Field(description="Include the OCR/text content (paged)")
        ] = False,
        content_offset: Annotated[int, Field(ge=0)] = 0,
        content_limit: Annotated[int, Field(ge=1, le=200_000)] = 20_000,
    ) -> dict[str, Any]:
        """Get a document's metadata (tags, correspondent, custom fields, ...) and optionally
        its text content. Long content is paged: pass content_next_offset back as
        content_offset to continue."""
        doc = await pc.get(f"documents/{document_id}")
        names = await Names(pc).load(*DOC_KINDS)
        return document_detail(
            doc,
            names,
            content_offset=content_offset,
            content_limit=content_limit if include_content else None,
        )

    @mcp.tool(annotations=READ)
    async def get_document_metadata(document_id: DocId) -> dict[str, Any]:
        """File-level metadata: checksums, sizes, original/archive file names, language and
        embedded PDF/EXIF metadata."""
        return await pc.get(f"documents/{document_id}/metadata")

    @mcp.tool(annotations=READ)
    async def get_document_suggestions(document_id: DocId) -> dict[str, Any]:
        """Paperless' classifier suggestions (correspondent, tags, type, storage path, dates)
        for a document, with names resolved."""
        raw = await pc.get(f"documents/{document_id}/suggestions")
        names = await Names(pc).load("tags", "correspondents", "document_types", "storage_paths")
        out: dict[str, Any] = {}
        for key, kind in (
            ("correspondents", "correspondents"),
            ("tags", "tags"),
            ("document_types", "document_types"),
            ("storage_paths", "storage_paths"),
        ):
            if raw.get(key):
                out[key] = [names.ref(kind, i) for i in raw[key]]
        if raw.get("dates"):
            out["dates"] = raw["dates"]
        return out

    @mcp.tool(annotations=READ)
    async def get_document_history(document_id: DocId) -> list[dict[str, Any]]:
        """Audit trail of changes to a document (requires the audit log to be enabled)."""
        return await pc.get(f"documents/{document_id}/history") or []

    @mcp.tool(annotations=READ)
    async def find_similar_documents(
        document_id: DocId, page: Page = 1, page_size: PageSize = 10
    ) -> dict[str, Any]:
        """Documents with content similar to the given document ("more like this")."""
        body = await pc.page(
            "documents",
            {"more_like_id": document_id, "fields": LIST_FIELDS, "truncate_content": "true"},
            page=page,
            page_size=page_size,
        )
        return page_result(body, page, await summaries(pc, body.get("results") or []))

    @mcp.tool(annotations=READ, output_schema=None)
    async def get_document_thumbnail(document_id: DocId) -> Image:
        """A small image of the document's first page."""
        resp = await pc.get(f"documents/{document_id}/thumb", expect="response")
        mime = (resp.headers.get("content-type") or "image/webp").split(";")[0]
        return Image(data=resp.content, format=mime.removeprefix("image/"))

    @mcp.tool(annotations=READ, output_schema=None)
    async def download_document(
        document_id: DocId,
        original: Annotated[
            bool, Field(description="The originally uploaded file instead of the archived PDF")
        ] = False,
    ) -> Any:
        """Return a document's file inline (images as images, text as text, PDFs and others
        as embedded files). Limited to MCP_MAX_INLINE_MB; for larger files use
        create_download_link."""
        params = {"original": "true"} if original else None
        async with pc.stream("GET", f"documents/{document_id}/download", params=params) as resp:
            declared = int(resp.headers.get("content-length") or 0)
            if declared > max_inline:
                raise PaperlessError(
                    413,
                    f"file is {_mb(declared)}; the inline limit is {_mb(max_inline)}. "
                    "Use create_download_link to fetch it with curl instead.",
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_inline:
                    raise PaperlessError(
                        413,
                        f"file exceeds the inline limit of {_mb(max_inline)}; "
                        "use create_download_link instead.",
                    )
                chunks.append(chunk)
            mime = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0]
        content = b"".join(chunks)
        if mime.startswith("image/"):
            return Image(data=content, format=mime.removeprefix("image/"))
        if mime.startswith("text/"):
            return content.decode("utf-8", errors="replace")
        sub = mime.split("/", 1)[-1]
        return File(data=content, format=sub, name=f"document-{document_id}")

    @mcp.tool(annotations=READ)
    async def get_next_asn() -> int:
        """The next free archive serial number (ASN)."""
        return int(await pc.get("documents/next_asn"))

    @mcp.tool(annotations=READ)
    async def list_trash(page: Page = 1, page_size: PageSize = 25) -> dict[str, Any]:
        """Documents in the trash (deleted but restorable until the trash is emptied)."""
        body = await pc.page("trash", page=page, page_size=page_size)
        docs = await summaries(pc, body.get("results") or [])
        for summary, raw in zip(docs, body.get("results") or [], strict=False):
            if raw.get("deleted_at"):
                summary["deleted_at"] = raw["deleted_at"]
        return page_result(body, page, docs)

    if settings.mcp_read_only:
        return

    # ----------------------------------------------------------------- write
    @mcp.tool(annotations=UPDATE)
    async def update_document(
        document_id: DocId,
        title: str | None = None,
        created: Date = None,
        correspondent: ClearableRef = None,
        document_type: ClearableRef = None,
        storage_path: ClearableRef = None,
        tags: Annotated[RefList, Field(description="Replace ALL tags with exactly these")] = None,
        add_tags: RefList = None,
        remove_tags: RefList = None,
        archive_serial_number: Annotated[
            int | None, Field(description="New ASN; 0 clears it")
        ] = None,
        custom_fields: CustomFieldValues = None,
        remove_inbox_tags: Annotated[
            bool, Field(description="Also strip inbox tags (marks the document as processed)")
        ] = False,
    ) -> dict[str, Any]:
        """Change a document's metadata. Only the fields you pass are changed. Tags,
        correspondent, type, storage path and custom fields accept names or ids."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if created is not None:
            body["created"] = created
        for kind, key, value in (
            ("correspondents", "correspondent", correspondent),
            ("document_types", "document_type", document_type),
            ("storage_paths", "storage_path", storage_path),
        ):
            resolved = await pc.resolve_optional(kind, value)
            if resolved is not None:
                body[key] = resolved or None
        if archive_serial_number is not None:
            body["archive_serial_number"] = archive_serial_number or None
        if remove_inbox_tags:
            body["remove_inbox_tags"] = True

        current: dict[str, Any] | None = None
        if tags is not None or add_tags or remove_tags or custom_fields:
            current = await pc.get(
                f"documents/{document_id}", params={"fields": "tags,custom_fields"}
            )
        if tags is not None or add_tags or remove_tags:
            assert current is not None
            new_tags = (
                await pc.resolve_ids("tags", tags) if tags is not None else list(current["tags"])
            )
            for t in await pc.resolve_ids("tags", add_tags):
                if t not in new_tags:
                    new_tags.append(t)
            removing = set(await pc.resolve_ids("tags", remove_tags))
            body["tags"] = [t for t in new_tags if t not in removing]
        if custom_fields:
            assert current is not None
            merged = {cf["field"]: cf.get("value") for cf in current.get("custom_fields") or []}
            to_remove = {k for k, v in custom_fields.items() if v is None}
            to_set = {k: v for k, v in custom_fields.items() if v is not None}
            for key in to_remove:
                merged.pop(await pc.resolve_id("custom_fields", key), None)
            merged.update(await custom_field_values(pc, to_set))
            body["custom_fields"] = [{"field": k, "value": v} for k, v in merged.items()]
        if not body:
            raise PaperlessError(400, "nothing to update: pass at least one field")
        doc = await pc.patch(f"documents/{document_id}", json=body)
        names = await Names(pc).load(*DOC_KINDS)
        return document_detail(doc, names)

    @mcp.tool(annotations=DELETE)
    async def delete_document(document_id: DocId) -> dict[str, Any]:
        """Move a document to the trash. It can be restored with restore_documents until
        the trash is emptied."""
        await pc.delete(f"documents/{document_id}")
        return {"trashed": document_id}

    @mcp.tool(annotations=UPDATE)
    async def restore_documents(document_ids: list[int]) -> dict[str, Any]:
        """Restore documents from the trash."""
        await pc.post("trash", json={"action": "restore", "documents": document_ids})
        return {"restored": document_ids}

    @mcp.tool(annotations=DELETE)
    async def empty_trash(
        document_ids: Annotated[
            list[int] | None,
            Field(description="Permanently delete only these; omit to empty the whole trash"),
        ] = None,
    ) -> dict[str, Any]:
        """PERMANENTLY delete documents from the trash. This cannot be undone."""
        body: dict[str, Any] = {"action": "empty"}
        if document_ids:
            body["documents"] = document_ids
        result = await pc.post("trash", json=body)
        return drop_empty({"emptied": (result or {}).get("doc_ids") or document_ids or "all"})
