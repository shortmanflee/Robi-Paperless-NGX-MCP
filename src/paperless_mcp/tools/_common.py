"""Shared helpers for tool modules: annotations, argument types and response shaping."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from paperless_mcp.client import PaperlessClient

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
UPDATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
# Destructive but not idempotent (e.g. PDF edits that replace/delete originals).
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

Ref = int | str
DocId = Annotated[int, Field(description="Paperless document id")]
RefArg = Annotated[Ref | None, Field(description="Id or exact name")]
RefList = Annotated[list[Ref] | None, Field(description="Ids or exact names")]
ClearableRef = Annotated[
    Ref | None, Field(description="Id or exact name; '' clears it; omit to leave unchanged")
]
Date = Annotated[str | None, Field(description="Date as YYYY-MM-DD")]
CustomFieldValues = Annotated[
    dict[str, Any] | None,
    Field(
        description="{custom field name or id: value}. Select fields accept the option label. "
        "Use null as the value to remove the field from the document (update only)."
    ),
]
Page = Annotated[int, Field(ge=1, description="1-based page number")]
PageSize = Annotated[int, Field(ge=1, le=100)]

MATCHING_ALGORITHMS = ("none", "any", "all", "literal", "regex", "fuzzy", "auto")


def drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Remove None values and empty strings/lists to keep responses compact."""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != []}


def page_result(body: dict[str, Any], page: int, items: list[Any]) -> dict[str, Any]:
    return {
        "count": body.get("count", len(items)),
        "page": page,
        "has_more": bool(body.get("next")),
        "results": items,
    }


class Names:
    """Id->name maps for the taxonomy kinds, fetched lazily (and cached by the client)."""

    def __init__(self, pc: PaperlessClient):
        self.pc = pc
        self._maps: dict[str, dict[int, str]] = {}

    async def load(self, *kinds: str) -> Names:
        for kind in kinds:
            if kind not in self._maps:
                self._maps[kind] = await self.pc.names(kind)
        return self

    def name(self, kind: str, value: Any) -> str | None:
        if value is None:
            return None
        return self._maps.get(kind, {}).get(int(value), f"#{value}")

    def ref(self, kind: str, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"id": value, "name": self.name(kind, value)}


DOC_KINDS = ("tags", "correspondents", "document_types", "storage_paths", "custom_fields")


def summarize_document(d: dict[str, Any], names: Names) -> dict[str, Any]:
    hit = d.get("__search_hit__") or {}
    return drop_empty(
        {
            "id": d.get("id"),
            "title": d.get("title"),
            "created": d.get("created_date") or d.get("created"),
            "correspondent": names.name("correspondents", d.get("correspondent")),
            "document_type": names.name("document_types", d.get("document_type")),
            "storage_path": names.name("storage_paths", d.get("storage_path")),
            "tags": [names.name("tags", t) for t in d.get("tags") or []],
            "asn": d.get("archive_serial_number"),
            "page_count": d.get("page_count"),
            "score": round(hit["score"], 3) if hit.get("score") is not None else None,
            "highlights": hit.get("highlights") or None,
            "note_highlights": hit.get("note_highlights") or None,
        }
    )


def custom_field_list(d: dict[str, Any], names: Names) -> dict[str, Any]:
    return {
        names.name("custom_fields", cf.get("field")) or str(cf.get("field")): cf.get("value")
        for cf in d.get("custom_fields") or []
    }


def document_detail(
    d: dict[str, Any],
    names: Names,
    *,
    content_offset: int = 0,
    content_limit: int | None = None,
) -> dict[str, Any]:
    content = d.get("content")
    detail: dict[str, Any] = {
        "id": d.get("id"),
        "title": d.get("title"),
        "created": d.get("created_date") or d.get("created"),
        "added": d.get("added"),
        "modified": d.get("modified"),
        "correspondent": names.ref("correspondents", d.get("correspondent")),
        "document_type": names.ref("document_types", d.get("document_type")),
        "storage_path": names.ref("storage_paths", d.get("storage_path")),
        "tags": [names.ref("tags", t) for t in d.get("tags") or []],
        "custom_fields": custom_field_list(d, names) or None,
        "archive_serial_number": d.get("archive_serial_number"),
        "original_file_name": d.get("original_file_name"),
        "archived_file_name": d.get("archived_file_name"),
        "mime_type": d.get("mime_type"),
        "page_count": d.get("page_count"),
        "owner": d.get("owner"),
        "notes_count": len(d.get("notes") or []) or None,
        "duplicate_documents": [x.get("id") for x in d.get("duplicate_documents") or []] or None,
        "root_document": d.get("root_document"),
        "versions": d.get("versions") or None,
        "deleted_at": d.get("deleted_at"),
    }
    if content is not None and content_limit is not None:
        total = len(content)
        chunk = content[content_offset : content_offset + content_limit]
        detail["content"] = chunk
        detail["content_length"] = total
        end = content_offset + len(chunk)
        if end < total:
            detail["content_next_offset"] = end
    return drop_empty(detail)


def matching_fields(
    match: str | None, matching_algorithm: str | None, is_insensitive: bool | None
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if match is not None:
        body["match"] = match
    if matching_algorithm is not None:
        body["matching_algorithm"] = MATCHING_ALGORITHMS.index(matching_algorithm)
    if is_insensitive is not None:
        body["is_insensitive"] = is_insensitive
    return body


def matching_out(o: dict[str, Any]) -> dict[str, Any]:
    algo = o.get("matching_algorithm")
    return {
        "match": o.get("match") or None,
        "matching_algorithm": MATCHING_ALGORITHMS[algo]
        if isinstance(algo, int) and 0 <= algo < len(MATCHING_ALGORITHMS)
        else algo,
        "is_insensitive": o.get("is_insensitive"),
    }
