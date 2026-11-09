"""Public share links for documents (served by Paperless at /share/<slug>)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, WRITE, DocId, drop_empty

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    def _link(s: dict[str, Any]) -> dict[str, Any]:
        return drop_empty(
            {
                "id": s.get("id"),
                "url": f"{pc.public_url}/share/{s.get('slug')}" if s.get("slug") else None,
                "document": s.get("document"),
                "document_title": s.get("document_title"),
                "file_version": s.get("file_version"),
                "created": s.get("created"),
                "expiration": s.get("expiration") or "never",
            }
        )

    @mcp.tool(annotations=READ)
    async def list_share_links(
        document_id: Annotated[
            int | None, Field(description="Only links for this document")
        ] = None,
    ) -> list[dict[str, Any]]:
        """List public share links (anyone with the URL can download the document)."""
        if document_id is not None:
            rows = await pc.get(f"documents/{document_id}/share_links") or []
        else:
            rows = await pc.paginate("share_links")
        return [_link(s) for s in rows]

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE.model_copy(update={"open_world_hint": True}))
    async def create_share_link(
        document_id: DocId,
        expires_in_days: Annotated[
            int | None, Field(ge=1, description="Omit for a link that never expires")
        ] = 7,
        file_version: Literal["archive", "original"] = "archive",
    ) -> dict[str, Any]:
        """Create a public link that lets ANYONE with the URL download the document
        (no login). Reachable only where Paperless itself is reachable."""
        body: dict[str, Any] = {"document": document_id, "file_version": file_version}
        if expires_in_days:
            body["expiration"] = (datetime.now(UTC) + timedelta(days=expires_in_days)).isoformat()
        return _link(await pc.post("share_links", json=body))

    @mcp.tool(annotations=DELETE)
    async def delete_share_link(share_link_id: int) -> dict[str, Any]:
        """Revoke a share link."""
        await pc.delete(f"share_links/{share_link_id}")
        return {"deleted": share_link_id}
