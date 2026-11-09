"""Background tasks (document consumption and other jobs)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from paperless_mcp.client import PaperlessClient, PaperlessError
from paperless_mcp.ingest import get_task as fetch_task
from paperless_mcp.ingest import summarize_task, wait_for_task
from paperless_mcp.tools._common import READ, UPDATE, Page, PageSize, page_result

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

Status = Literal["pending", "started", "success", "failure", "revoked"]


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def get_task(
        task_id: Annotated[str, Field(description="Task UUID returned by an upload")],
        wait_seconds: Annotated[
            float, Field(ge=0, le=300, description="Wait up to this long for it to finish")
        ] = 0,
    ) -> dict[str, Any]:
        """Status of a background task, e.g. a document being consumed. On success it
        includes the new document_id; on failure the reason (e.g. duplicate_of)."""
        if wait_seconds:
            return await wait_for_task(pc, task_id, wait_seconds)
        task = await fetch_task(pc, task_id)
        if task is None:
            raise PaperlessError(404, f"no task {task_id}")
        return summarize_task(task)

    @mcp.tool(annotations=READ)
    async def list_tasks(
        status: list[Status] | None = None,
        acknowledged: Annotated[
            bool | None, Field(description="false = only tasks not yet dismissed")
        ] = None,
        task_type: Annotated[str | None, Field(description="e.g. consume_file")] = None,
        page: Page = 1,
        page_size: PageSize = 25,
    ) -> dict[str, Any]:
        """List recent background tasks, newest first."""
        params: dict[str, Any] = {
            "status": status,
            "acknowledged": None if acknowledged is None else str(acknowledged).lower(),
            "task_type": task_type,
            "ordering": "-date_created",
        }
        body = await pc.page("tasks", params, page=page, page_size=page_size)
        return page_result(body, page, [summarize_task(t) for t in body.get("results") or []])

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=UPDATE)
    async def acknowledge_tasks(
        task_ids: Annotated[
            list[int] | None,
            Field(description="Numeric task ids (the `id` field); omit to dismiss all"),
        ] = None,
    ) -> dict[str, Any]:
        """Dismiss finished tasks from the Paperless task list."""
        body: dict[str, Any] = {"tasks": task_ids} if task_ids else {"all": True}
        result = await pc.post("tasks/acknowledge", json=body)
        return {"acknowledged": (result or {}).get("result")}
