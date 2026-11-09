"""Tool registration. Each module exposes `register(mcp, pc, settings)`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from paperless_mcp.tools import (
    bulk,
    custom_fields,
    documents,
    files,
    mail,
    notes,
    reporting,
    saved_views,
    share_links,
    tasks,
    taxonomy,
    workflows,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.client import PaperlessClient
    from paperless_mcp.config import Settings
    from paperless_mcp.uploads import UploadStore

MODULES = (
    documents,
    notes,
    bulk,
    taxonomy,
    custom_fields,
    saved_views,
    share_links,
    workflows,
    mail,
    tasks,
    reporting,
)


def register_all(mcp: FastMCP, pc: PaperlessClient, settings: Settings, store: UploadStore) -> None:
    for module in MODULES:
        module.register(mcp, pc, settings)
    files.register(mcp, pc, settings, store)
