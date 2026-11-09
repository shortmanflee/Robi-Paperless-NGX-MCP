"""Tags, correspondents, document types and storage paths.

All four share Paperless' auto-matching fields (match, matching_algorithm, is_insensitive).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import UPDATE, WRITE, drop_empty, matching_fields, matching_out
from paperless_mcp.tools._taxonomy import Kind, register_common, save

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

Matching = Literal["none", "any", "all", "literal", "regex", "fuzzy", "auto"]
MatchArg = Annotated[
    str | None, Field(description="Text/pattern Paperless matches against document content")
]
AlgoArg = Annotated[
    Matching | None,
    Field(
        description="How `match` is applied: any/all words, literal, regex, fuzzy; 'auto' lets "
        "the classifier learn from assigned documents; 'none' disables auto-assignment"
    ),
]
Color = Annotated[str | None, Field(description="Hex colour, e.g. #ff8800")]
Ref = Annotated[int | str, Field(description="Id or exact name")]
PathArg = Annotated[
    str,
    Field(
        description="Jinja-style file path template, e.g. "
        "'{{ correspondent }}/{{ created_year }}/{{ title }}'"
    ),
]


def _base(o: dict[str, Any]) -> dict[str, Any]:
    return {"id": o.get("id"), "name": o.get("name"), "document_count": o.get("document_count")}


def _tag(o: dict[str, Any]) -> dict[str, Any]:
    children = o.get("children") or []
    return drop_empty(
        _base(o)
        | {
            "color": o.get("color"),
            "is_inbox_tag": o.get("is_inbox_tag") or None,
            "parent": o.get("parent"),
            "children": [
                {"id": c.get("id"), "name": c.get("name")} if isinstance(c, dict) else c
                for c in children
            ],
        }
        | matching_out(o)
    )


def _correspondent(o: dict[str, Any]) -> dict[str, Any]:
    return drop_empty(
        _base(o) | {"last_correspondence": o.get("last_correspondence")} | matching_out(o)
    )


def _document_type(o: dict[str, Any]) -> dict[str, Any]:
    return drop_empty(_base(o) | matching_out(o))


def _storage_path(o: dict[str, Any]) -> dict[str, Any]:
    return drop_empty(_base(o) | {"path": o.get("path")} | matching_out(o))


TAGS = Kind("tags", "tag", "tags", "tag", _tag, "Documents keep existing but lose the tag.")
CORRESPONDENTS = Kind(
    "correspondents",
    "correspondent",
    "correspondents",
    "correspondent",
    _correspondent,
    "Documents keep existing; their correspondent is cleared.",
)
DOCUMENT_TYPES = Kind(
    "document_types",
    "document_type",
    "document_types",
    "document type",
    _document_type,
    "Documents keep existing; their type is cleared.",
)
STORAGE_PATHS = Kind(
    "storage_paths",
    "storage_path",
    "storage_paths",
    "storage path",
    _storage_path,
    "Documents keep existing and fall back to the default storage layout.",
)


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    for kind in (TAGS, CORRESPONDENTS, DOCUMENT_TYPES, STORAGE_PATHS):
        register_common(mcp, pc, settings, kind)
    if settings.mcp_read_only:
        return

    # ------------------------------------------------------------------ tags
    async def _tag_body(
        name, color, is_inbox_tag, parent, match, matching_algorithm, is_insensitive
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "color": color, "is_inbox_tag": is_inbox_tag}
        body = {k: v for k, v in body.items() if v is not None}
        if parent is not None:
            body["parent"] = (await pc.resolve_id("tags", parent)) if parent != "" else None
        return body | matching_fields(match, matching_algorithm, is_insensitive)

    @mcp.tool(annotations=WRITE)
    async def create_tag(
        name: str,
        color: Color = None,
        is_inbox_tag: Annotated[
            bool | None, Field(description="Automatically add to every new document")
        ] = None,
        parent: Annotated[int | str | None, Field(description="Parent tag id or name")] = None,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Create a tag (optionally nested under a parent tag)."""
        body = await _tag_body(
            name, color, is_inbox_tag, parent, match, matching_algorithm, is_insensitive
        )
        return await save(pc, TAGS, body)

    @mcp.tool(annotations=UPDATE)
    async def update_tag(
        tag: Ref,
        name: str | None = None,
        color: Color = None,
        is_inbox_tag: bool | None = None,
        parent: Annotated[
            int | str | None, Field(description="Parent tag id or name; '' to un-nest")
        ] = None,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Update a tag. Only the fields you pass change."""
        body = await _tag_body(
            name, color, is_inbox_tag, parent, match, matching_algorithm, is_insensitive
        )
        return await save(pc, TAGS, body, tag)

    # -------------------------------------------------------- correspondents
    @mcp.tool(annotations=WRITE)
    async def create_correspondent(
        name: str,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Create a correspondent (a person or organisation documents come from)."""
        body = {"name": name} | matching_fields(match, matching_algorithm, is_insensitive)
        return await save(pc, CORRESPONDENTS, body)

    @mcp.tool(annotations=UPDATE)
    async def update_correspondent(
        correspondent: Ref,
        name: str | None = None,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Rename a correspondent or change its matching rule."""
        body = ({"name": name} if name else {}) | matching_fields(
            match, matching_algorithm, is_insensitive
        )
        return await save(pc, CORRESPONDENTS, body, correspondent)

    # -------------------------------------------------------- document types
    @mcp.tool(annotations=WRITE)
    async def create_document_type(
        name: str,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Create a document type (e.g. Invoice, Contract, Payslip)."""
        body = {"name": name} | matching_fields(match, matching_algorithm, is_insensitive)
        return await save(pc, DOCUMENT_TYPES, body)

    @mcp.tool(annotations=UPDATE)
    async def update_document_type(
        document_type: Ref,
        name: str | None = None,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Rename a document type or change its matching rule."""
        body = ({"name": name} if name else {}) | matching_fields(
            match, matching_algorithm, is_insensitive
        )
        return await save(pc, DOCUMENT_TYPES, body, document_type)

    # --------------------------------------------------------- storage paths
    @mcp.tool(annotations=WRITE)
    async def create_storage_path(
        name: str,
        path: PathArg,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Create a storage path (where matching documents' files are stored on disk)."""
        body = {"name": name, "path": path} | matching_fields(
            match, matching_algorithm, is_insensitive
        )
        return await save(pc, STORAGE_PATHS, body)

    @mcp.tool(annotations=UPDATE)
    async def update_storage_path(
        storage_path: Ref,
        name: str | None = None,
        path: Annotated[str | None, Field(description="New path template")] = None,
        match: MatchArg = None,
        matching_algorithm: AlgoArg = None,
        is_insensitive: bool | None = None,
    ) -> dict[str, Any]:
        """Update a storage path. Changing the template moves existing files."""
        body = {k: v for k, v in {"name": name, "path": path}.items() if v} | matching_fields(
            match, matching_algorithm, is_insensitive
        )
        return await save(pc, STORAGE_PATHS, body, storage_path)
