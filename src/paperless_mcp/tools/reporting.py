"""Statistics and server status."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.tools._common import READ, drop_empty

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings


def _no_urls(section: Any) -> Any:
    """Drop connection URLs (they can embed credentials) from status sections."""
    if not isinstance(section, dict):
        return section
    return {k: v for k, v in section.items() if not k.endswith("url")}


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def get_statistics() -> dict[str, Any]:
        """Document counts, inbox size, character count, file types and totals per
        tag/correspondent/type/storage path."""
        return await pc.get("statistics")

    @mcp.tool(annotations=READ)
    async def get_server_status() -> dict[str, Any]:
        """Paperless version and health (database, Redis, Celery, search index, classifier,
        storage). Detailed status needs an admin account; otherwise only versions."""
        resp = await pc.get("ui_settings", expect="response")
        out: dict[str, Any] = {
            "paperless_url": pc.base_url,
            "paperless_version": pc.server_version or resp.headers.get("x-version"),
            "api_version_latest": pc.server_api_version,
            "api_version_used": pc.api_version,
        }
        try:
            status = await pc.get("status")
        except PaperlessError as exc:
            out["status"] = f"unavailable ({exc.message})"
        else:
            out |= drop_empty(
                {
                    "storage": status.get("storage"),
                    "database": _no_urls(status.get("database")),
                    "tasks": _no_urls(status.get("tasks")),
                    "install_type": status.get("install_type"),
                    "server_os": status.get("server_os"),
                }
            )
        return out
