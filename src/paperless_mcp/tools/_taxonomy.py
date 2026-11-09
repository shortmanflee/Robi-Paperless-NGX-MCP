"""Generic list/get/delete tools for Paperless' named objects (tags, correspondents, ...).

Create/update tools differ per kind (tags have colours and parents, storage paths have a
path template), so each module defines those explicitly and uses `save()` from here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, Page, PageSize, page_result

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

Shape = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Kind:
    endpoint: str  # API path, e.g. "document_types"
    singular: str  # tool suffix, e.g. "document_type"
    plural: str  # tool suffix, e.g. "document_types"
    label: str  # human name, e.g. "document type"
    shape: Shape
    delete_note: str = ""


def register_common(mcp: FastMCP, pc: PaperlessClient, settings: Settings, kind: Kind) -> None:
    async def list_objects(
        name_contains: Annotated[str | None, Field(description="Filter by name")] = None,
        page: Page = 1,
        page_size: PageSize = 100,
    ) -> dict[str, Any]:
        params = {"name__icontains": name_contains, "ordering": "name"}
        body = await pc.page(kind.endpoint, params, page=page, page_size=page_size)
        return page_result(body, page, [kind.shape(o) for o in body.get("results") or []])

    async def get_object(
        ref: Annotated[int | str, Field(description="Id or exact name")],
    ) -> dict[str, Any]:
        obj_id = await pc.resolve_id(kind.endpoint, ref)
        return kind.shape(await pc.get(f"{kind.endpoint}/{obj_id}"))

    mcp.tool(
        list_objects,
        name=f"list_{kind.plural}",
        description=f"List {kind.label}s (with document counts), optionally filtered by name.",
        annotations=READ,
    )
    mcp.tool(
        get_object,
        name=f"get_{kind.singular}",
        description=f"Get one {kind.label} by id or name, including its auto-matching rule.",
        annotations=READ,
    )
    if settings.mcp_read_only:
        return

    async def delete_object(
        ref: Annotated[int | str, Field(description="Id or exact name")],
    ) -> dict[str, Any]:
        obj_id = await pc.resolve_id(kind.endpoint, ref)
        await pc.delete(f"{kind.endpoint}/{obj_id}")
        pc.invalidate(kind.endpoint)
        return {"deleted": obj_id}

    mcp.tool(
        delete_object,
        name=f"delete_{kind.singular}",
        description=f"Delete a {kind.label}. {kind.delete_note}".strip(),
        annotations=DELETE,
    )


async def save(
    pc: PaperlessClient, kind: Kind, body: dict[str, Any], ref: int | str | None = None
) -> dict[str, Any]:
    """POST a new object, or PATCH an existing one (only the given fields change)."""
    if ref is None:
        obj = await pc.post(kind.endpoint, json=body)
    else:
        obj_id = await pc.resolve_id(kind.endpoint, ref)
        obj = await pc.patch(f"{kind.endpoint}/{obj_id}", json=body)
    pc.invalidate(kind.endpoint)
    return kind.shape(obj)
