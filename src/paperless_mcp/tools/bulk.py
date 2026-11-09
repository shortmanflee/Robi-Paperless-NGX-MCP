"""Bulk metadata edits and PDF operations (merge, split, rotate, delete pages)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field

from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.ingest import custom_field_values
from paperless_mcp.tools._common import (
    DELETE,
    DESTRUCTIVE,
    WRITE,
    ClearableRef,
    DocId,
    RefList,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

DocIds = Annotated[list[int], Field(min_length=1, description="Document ids")]


class PageOp(BaseModel):
    page: int = Field(ge=1, description="1-based page number in the source document")
    rotate: int | None = Field(default=None, description="Clockwise degrees, multiple of 90")
    doc: int | None = Field(default=None, ge=0, description="Output document index (splits)")


def _result(body: Any, **extra: Any) -> dict[str, Any]:
    out = dict(body) if isinstance(body, dict) else {"result": body}
    return out | {k: v for k, v in extra.items() if v is not None}


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    if settings.mcp_read_only:
        return

    async def _bulk(documents: list[int], method: str, parameters: dict[str, Any]) -> Any:
        return await pc.post(
            "documents/bulk_edit",
            json={"documents": documents, "method": method, "parameters": parameters},
        )

    @mcp.tool(annotations=WRITE)
    async def bulk_edit_documents(
        document_ids: DocIds,
        add_tags: RefList = None,
        remove_tags: RefList = None,
        correspondent: ClearableRef = None,
        document_type: ClearableRef = None,
        storage_path: ClearableRef = None,
        set_custom_fields: Annotated[
            dict[str, Any] | None,
            Field(description="{custom field name or id: value} to set on every document"),
        ] = None,
        remove_custom_fields: RefList = None,
    ) -> dict[str, Any]:
        """Change metadata on many documents at once: add/remove tags, set or clear the
        correspondent, type or storage path, and set/remove custom fields."""
        applied: list[str] = []
        if add_tags or remove_tags:
            await _bulk(
                document_ids,
                "modify_tags",
                {
                    "add_tags": await pc.resolve_ids("tags", add_tags),
                    "remove_tags": await pc.resolve_ids("tags", remove_tags),
                },
            )
            applied.append("tags")
        for kind, method, key, value in (
            ("correspondents", "set_correspondent", "correspondent", correspondent),
            ("document_types", "set_document_type", "document_type", document_type),
            ("storage_paths", "set_storage_path", "storage_path", storage_path),
        ):
            resolved = await pc.resolve_optional(kind, value)
            if resolved is not None:
                await _bulk(document_ids, method, {key: resolved or None})
                applied.append(key)
        if set_custom_fields or remove_custom_fields:
            values = await custom_field_values(pc, set_custom_fields or {})
            await _bulk(
                document_ids,
                "modify_custom_fields",
                {
                    "add_custom_fields": {str(k): v for k, v in values.items()},
                    "remove_custom_fields": await pc.resolve_ids(
                        "custom_fields", remove_custom_fields
                    ),
                },
            )
            applied.append("custom_fields")
        if not applied:
            raise PaperlessError(400, "nothing to change: pass at least one edit")
        return {"documents": document_ids, "applied": applied}

    @mcp.tool(annotations=DELETE)
    async def bulk_delete_documents(document_ids: DocIds) -> dict[str, Any]:
        """Move several documents to the trash."""
        return _result(await pc.post("documents/delete", json={"documents": document_ids}))

    @mcp.tool(annotations=WRITE)
    async def reprocess_documents(document_ids: DocIds) -> dict[str, Any]:
        """Re-run OCR and parsing on documents from their original files (runs in the
        background; metadata is kept)."""
        return _result(await pc.post("documents/reprocess", json={"documents": document_ids}))

    @mcp.tool(annotations=WRITE)
    async def rotate_documents(
        document_ids: DocIds,
        degrees: Annotated[int, Field(description="Clockwise: 90, 180 or 270")],
    ) -> dict[str, Any]:
        """Rotate every page of the given documents."""
        if degrees % 90:
            raise PaperlessError(400, "degrees must be a multiple of 90")
        return _result(
            await pc.post("documents/rotate", json={"documents": document_ids, "degrees": degrees})
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def merge_documents(
        document_ids: Annotated[
            list[int], Field(min_length=2, description="Documents in the order to merge")
        ],
        metadata_document_id: Annotated[
            int | None, Field(description="Copy title/tags/etc. from this document")
        ] = None,
        delete_originals: Annotated[
            bool, Field(description="Trash the source documents afterwards")
        ] = False,
    ) -> dict[str, Any]:
        """Merge documents into one new PDF document (consumed in the background)."""
        body: dict[str, Any] = {"documents": document_ids, "delete_originals": delete_originals}
        if metadata_document_id is not None:
            body["metadata_document_id"] = metadata_document_id
        return _result(await pc.post("documents/merge", json=body))

    async def _edit_pdf(
        document_id: int,
        operations: list[dict[str, Any]],
        *,
        update_document: bool,
        delete_original: bool,
    ) -> dict[str, Any]:
        body = {
            "documents": [document_id],
            "operations": operations,
            "update_document": update_document,
            "delete_original": delete_original,
            "include_metadata": True,
        }
        return _result(await pc.post("documents/edit_pdf", json=body))

    async def _page_count(document_id: int) -> int:
        doc = await pc.get(f"documents/{document_id}", params={"fields": "id,page_count"})
        count = doc.get("page_count")
        if not count:
            raise PaperlessError(409, f"document {document_id} has no page count (not a PDF?)")
        return int(count)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def edit_document_pages(
        document_id: DocId,
        pages: Annotated[
            list[PageOp],
            Field(
                description="The pages to keep, in output order, each optionally rotated and "
                "assigned to an output document index (doc) for splitting"
            ),
        ],
        update_document: Annotated[
            bool, Field(description="Replace this document's file (single output only)")
        ] = False,
        delete_original: Annotated[
            bool, Field(description="Trash the original after creating new documents")
        ] = False,
    ) -> dict[str, Any]:
        """Low-level PDF page editor: reorder, rotate, drop or split pages. Pages not listed
        are dropped. Without update_document, results are added as new documents."""
        ops = [p.model_dump(exclude_none=True) for p in pages]
        return await _edit_pdf(
            document_id, ops, update_document=update_document, delete_original=delete_original
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_pages(
        document_id: DocId,
        pages: Annotated[list[int], Field(min_length=1, description="1-based pages to remove")],
    ) -> dict[str, Any]:
        """Remove pages from a document (its file is replaced)."""
        total = await _page_count(document_id)
        drop = set(pages)
        keep = [{"page": p} for p in range(1, total + 1) if p not in drop]
        if not keep:
            raise PaperlessError(400, "cannot remove every page; delete the document instead")
        return await _edit_pdf(document_id, keep, update_document=True, delete_original=False)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def split_document(
        document_id: DocId,
        split_after: Annotated[
            list[int],
            Field(min_length=1, description="Start a new document after each of these pages"),
        ],
        delete_original: bool = False,
    ) -> dict[str, Any]:
        """Split a document into several new documents, e.g. split_after=[2, 5] on a
        7-page PDF gives pages 1-2, 3-5 and 6-7."""
        total = await _page_count(document_id)
        cuts = sorted({p for p in split_after if 1 <= p < total})
        if not cuts:
            raise PaperlessError(400, f"split_after must contain pages between 1 and {total - 1}")
        ops: list[dict[str, Any]] = []
        doc_index = 0
        for page in range(1, total + 1):
            ops.append({"page": page, "doc": doc_index})
            if page in cuts:
                doc_index += 1
        return await _edit_pdf(
            document_id, ops, update_document=False, delete_original=delete_original
        )
