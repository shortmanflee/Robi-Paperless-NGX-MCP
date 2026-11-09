"""Notes on documents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, WRITE, DocId, drop_empty

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings


def _note(n: dict[str, Any]) -> dict[str, Any]:
    user = n.get("user")
    if isinstance(user, dict):
        user = user.get("username") or user.get("id")
    return drop_empty(
        {"id": n.get("id"), "note": n.get("note"), "created": n.get("created"), "user": user}
    )


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def list_notes(document_id: DocId) -> list[dict[str, Any]]:
        """List the notes on a document."""
        return [_note(n) for n in await pc.get(f"documents/{document_id}/notes") or []]

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE)
    async def add_note(document_id: DocId, note: str) -> list[dict[str, Any]]:
        """Add a note to a document. Returns all notes on the document."""
        notes = await pc.post(f"documents/{document_id}/notes", json={"note": note})
        return [_note(n) for n in notes or []]

    @mcp.tool(annotations=DELETE)
    async def delete_note(document_id: DocId, note_id: int) -> dict[str, Any]:
        """Delete a note from a document."""
        await pc.request(
            "DELETE", f"documents/{document_id}/notes", params={"id": note_id}, expect="none"
        )
        return {"deleted": note_id}
