"""Workflows: triggers (when) + actions (what) applied to documents automatically."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, UPDATE, WRITE

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

TRIGGER_HELP = (
    "List of trigger objects as accepted by the Paperless API. Key fields: "
    "type (1 consumption started, 2 document added, 3 document updated, 4 scheduled), "
    "sources (1 consume folder, 2 API upload, 3 mail fetch, 4 web UI), filter_filename "
    "(glob), filter_path, filter_mailrule, match + matching_algorithm (0-6), "
    "filter_has_tags / filter_has_all_tags / filter_has_not_tags (tag ids), "
    "filter_has_document_type, filter_has_correspondent, filter_custom_field_query; "
    "scheduled: schedule_date_field, schedule_offset_days, schedule_is_recurring."
)
ACTION_HELP = (
    "List of action objects as accepted by the Paperless API. Key fields: "
    "type (1 assignment, 2 removal, 3 email, 4 webhook); assignment: assign_title "
    "(template), assign_tags, assign_correspondent, assign_document_type, "
    "assign_storage_path, assign_owner, assign_custom_fields, assign_custom_fields_values; "
    "removal: remove_tags, remove_all_tags, remove_correspondents, ...; "
    "email: {subject, body, to, include_document}; webhook: {url, use_params, body, ...}."
)


def _workflow(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": w.get("id"),
        "name": w.get("name"),
        "order": w.get("order"),
        "enabled": w.get("enabled"),
        "triggers": w.get("triggers"),
        "actions": w.get("actions"),
    }


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def list_workflows() -> list[dict[str, Any]]:
        """List workflows with their triggers and actions."""
        return [_workflow(w) for w in await pc.paginate("workflows")]

    @mcp.tool(annotations=READ)
    async def get_workflow(workflow_id: int) -> dict[str, Any]:
        """Get one workflow with its triggers and actions."""
        return _workflow(await pc.get(f"workflows/{workflow_id}"))

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE)
    async def create_workflow(
        name: str,
        triggers: Annotated[list[dict[str, Any]], Field(min_length=1, description=TRIGGER_HELP)],
        actions: Annotated[list[dict[str, Any]], Field(min_length=1, description=ACTION_HELP)],
        order: int = 0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create a workflow. Tip: look at an existing one with get_workflow for the exact
        shape of triggers and actions."""
        body = {
            "name": name,
            "order": order,
            "enabled": enabled,
            "triggers": triggers,
            "actions": actions,
        }
        return _workflow(await pc.post("workflows", json=body))

    @mcp.tool(annotations=UPDATE)
    async def update_workflow(
        workflow_id: int,
        name: str | None = None,
        order: int | None = None,
        enabled: bool | None = None,
        triggers: Annotated[
            list[dict[str, Any]] | None,
            Field(description="Replaces ALL triggers. " + TRIGGER_HELP),
        ] = None,
        actions: Annotated[
            list[dict[str, Any]] | None,
            Field(description="Replaces ALL actions. " + ACTION_HELP),
        ] = None,
    ) -> dict[str, Any]:
        """Update a workflow (e.g. enable/disable it). Only the fields you pass change; to
        edit one trigger/action, pass the full list including its id."""
        body = {
            "name": name,
            "order": order,
            "enabled": enabled,
            "triggers": triggers,
            "actions": actions,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return _workflow(await pc.patch(f"workflows/{workflow_id}", json=body))

    @mcp.tool(annotations=DELETE)
    async def delete_workflow(workflow_id: int) -> dict[str, Any]:
        """Delete a workflow (documents are not affected)."""
        await pc.delete(f"workflows/{workflow_id}")
        return {"deleted": workflow_id}
