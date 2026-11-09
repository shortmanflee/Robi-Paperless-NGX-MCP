"""Saved views (stored document filters)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, UPDATE, WRITE, drop_empty

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings


class FilterRule(BaseModel):
    rule_type: int = Field(
        description="Paperless filter rule type, e.g. 0 title contains, 1 content contains, "
        "2 ASN is, 3 correspondent is, 4 document type is, 6 has tag, 17 does not have tag, "
        "19 title or content contains, 20 full-text query, 22 has tags in, "
        "25 storage path is, 42 custom fields query, 43 created to, 44 created from "
        "(see get_saved_view on an existing view for examples)"
    )
    value: str | None = Field(description="Rule value (ids as strings, text, or JSON)")


Rules = Annotated[list[FilterRule], Field(description="Filter rules (replaces existing ones)")]
SortField = Annotated[
    str | None, Field(description="e.g. created, added, title, correspondent__name")
]


def _view(v: dict[str, Any]) -> dict[str, Any]:
    return drop_empty(
        {
            "id": v.get("id"),
            "name": v.get("name"),
            "sort_field": v.get("sort_field"),
            "sort_reverse": v.get("sort_reverse"),
            "filter_rules": v.get("filter_rules"),
            "page_size": v.get("page_size"),
            "display_mode": v.get("display_mode"),
            "display_fields": v.get("display_fields"),
            "owner": v.get("owner"),
        }
    )


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def list_saved_views() -> list[dict[str, Any]]:
        """List saved views and their filter rules."""
        return [_view(v) for v in await pc.paginate("saved_views")]

    @mcp.tool(annotations=READ)
    async def get_saved_view(view_id: int) -> dict[str, Any]:
        """Get one saved view."""
        return _view(await pc.get(f"saved_views/{view_id}"))

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE)
    async def create_saved_view(
        name: str,
        filter_rules: Rules,
        sort_field: SortField = "created",
        sort_reverse: bool = True,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Create a saved view from filter rules."""
        body: dict[str, Any] = {
            "name": name,
            "filter_rules": [r.model_dump() for r in filter_rules],
            "sort_field": sort_field,
            "sort_reverse": sort_reverse,
        }
        if page_size:
            body["page_size"] = page_size
        return _view(await pc.post("saved_views", json=body))

    @mcp.tool(annotations=UPDATE)
    async def update_saved_view(
        view_id: int,
        name: str | None = None,
        filter_rules: Annotated[
            list[FilterRule] | None, Field(description="Replaces all filter rules")
        ] = None,
        sort_field: SortField = None,
        sort_reverse: bool | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Update a saved view. Only the fields you pass change."""
        body: dict[str, Any] = {
            "name": name,
            "sort_field": sort_field,
            "sort_reverse": sort_reverse,
            "page_size": page_size,
        }
        body = {k: v for k, v in body.items() if v is not None}
        if filter_rules is not None:
            body["filter_rules"] = [r.model_dump() for r in filter_rules]
        return _view(await pc.patch(f"saved_views/{view_id}", json=body))

    @mcp.tool(annotations=DELETE)
    async def delete_saved_view(view_id: int) -> dict[str, Any]:
        """Delete a saved view (documents are not affected)."""
        await pc.delete(f"saved_views/{view_id}")
        return {"deleted": view_id}
