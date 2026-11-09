"""Custom field definitions (values on documents are set via update_document)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, UPDATE, WRITE, drop_empty

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

DataType = Literal[
    "string",
    "longtext",
    "url",
    "date",
    "boolean",
    "integer",
    "float",
    "monetary",
    "documentlink",
    "select",
]
Ref = Annotated[int | str, Field(description="Custom field id or exact name")]
Options = Annotated[
    list[str] | None,
    Field(description="Select fields only: the option labels, in order (full list)"),
]
Currency = Annotated[
    str | None, Field(description="Monetary fields only: default ISO currency, e.g. EUR")
]


def _field(f: dict[str, Any]) -> dict[str, Any]:
    extra = f.get("extra_data") or {}
    return drop_empty(
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "data_type": f.get("data_type"),
            "select_options": [o.get("label") for o in extra.get("select_options") or []],
            "default_currency": extra.get("default_currency"),
            "document_count": f.get("document_count"),
        }
    )


def _options(labels: list[str], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep option ids for labels that already exist so document values stay valid."""
    by_label = {o.get("label"): o.get("id") for o in existing}
    return [{"label": label, "id": by_label.get(label)} for label in labels]


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def list_custom_fields() -> list[dict[str, Any]]:
        """List custom field definitions (name, data type, select options)."""
        return [_field(f) for f in await pc.paginate("custom_fields")]

    @mcp.tool(annotations=READ)
    async def get_custom_field(field: Ref) -> dict[str, Any]:
        """Get one custom field definition by id or name."""
        fid = await pc.resolve_id("custom_fields", field)
        return _field(await pc.get(f"custom_fields/{fid}"))

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE)
    async def create_custom_field(
        name: str,
        data_type: DataType,
        select_options: Options = None,
        default_currency: Currency = None,
    ) -> dict[str, Any]:
        """Create a custom field. Select fields need select_options."""
        extra: dict[str, Any] = {}
        if select_options:
            extra["select_options"] = _options(select_options, [])
        if default_currency:
            extra["default_currency"] = default_currency
        body: dict[str, Any] = {"name": name, "data_type": data_type}
        if extra:
            body["extra_data"] = extra
        pc.invalidate("custom_fields")
        return _field(await pc.post("custom_fields", json=body))

    @mcp.tool(annotations=UPDATE)
    async def update_custom_field(
        field: Ref,
        name: str | None = None,
        select_options: Options = None,
        default_currency: Currency = None,
    ) -> dict[str, Any]:
        """Rename a custom field or change its select options / default currency. Removing
        a select option clears it on documents that used it."""
        fid = await pc.resolve_id("custom_fields", field)
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if select_options is not None or default_currency is not None:
            cur = await pc.get(f"custom_fields/{fid}")
            extra = dict(cur.get("extra_data") or {})
            if select_options is not None:
                extra["select_options"] = _options(
                    select_options, extra.get("select_options") or []
                )
            if default_currency is not None:
                extra["default_currency"] = default_currency or None
            body["extra_data"] = extra
        pc.invalidate("custom_fields")
        return _field(await pc.patch(f"custom_fields/{fid}", json=body))

    @mcp.tool(annotations=DELETE)
    async def delete_custom_field(field: Ref) -> dict[str, Any]:
        """Delete a custom field definition AND its values on every document."""
        fid = await pc.resolve_id("custom_fields", field)
        await pc.delete(f"custom_fields/{fid}")
        pc.invalidate("custom_fields")
        return {"deleted": fid}
