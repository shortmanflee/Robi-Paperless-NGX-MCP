"""Mail accounts and mail rules (Paperless fetches documents from IMAP mailboxes)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from paperless_mcp.client import PaperlessClient
from paperless_mcp.tools._common import DELETE, READ, UPDATE, WRITE, drop_empty

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from paperless_mcp.config import Settings

Security = Literal["none", "ssl", "starttls"]
_SECURITY = {"none": 1, "ssl": 2, "starttls": 3}
_SECURITY_NAMES = {v: k for k, v in _SECURITY.items()}

RULE_FIELDS_HELP = (
    "Mail rule fields as accepted by the Paperless API, e.g. folder, filter_from, filter_to, "
    "filter_subject, filter_body, filter_attachment_filename_include/exclude, maximum_age "
    "(days), action (1 delete, 2 move, 3 mark read, 4 flag, 5 tag), action_parameter, "
    "assign_title_from (1 subject, 2 filename, 3 none), assign_tags (ids), "
    "assign_correspondent_from (1 none, 2 address, 3 name, 4 custom), assign_correspondent, "
    "assign_document_type, attachment_type (1 attachments only, 2 everything), "
    "consumption_scope (1 attachments, 2 .eml, 3 both), order, enabled, stop_processing."
)


def _account(a: dict[str, Any]) -> dict[str, Any]:
    return drop_empty(
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "imap_server": a.get("imap_server"),
            "imap_port": a.get("imap_port"),
            "imap_security": _SECURITY_NAMES.get(a.get("imap_security"), a.get("imap_security")),
            "username": a.get("username"),
            "character_set": a.get("character_set"),
            "is_token": a.get("is_token") or None,
            "account_type": a.get("account_type"),
        }
    )  # password intentionally omitted


def register(mcp: FastMCP, pc: PaperlessClient, settings: Settings) -> None:
    @mcp.tool(annotations=READ)
    async def list_mail_accounts() -> list[dict[str, Any]]:
        """List IMAP mail accounts (passwords are never returned)."""
        return [_account(a) for a in await pc.paginate("mail_accounts")]

    @mcp.tool(annotations=READ)
    async def list_mail_rules(
        account_id: Annotated[int | None, Field(description="Only rules for this account")] = None,
    ) -> list[dict[str, Any]]:
        """List mail rules (what to fetch from which folder and how to file it)."""
        rules = await pc.paginate("mail_rules")
        if account_id is not None:
            rules = [r for r in rules if r.get("account") == account_id]
        return [drop_empty({k: v for k, v in r.items() if k != "permissions"}) for r in rules]

    @mcp.tool(annotations=READ)
    async def get_mail_rule(rule_id: int) -> dict[str, Any]:
        """Get one mail rule."""
        r = await pc.get(f"mail_rules/{rule_id}")
        return drop_empty({k: v for k, v in r.items() if k != "permissions"})

    if settings.mcp_read_only:
        return

    @mcp.tool(annotations=WRITE)
    async def create_mail_account(
        name: str,
        imap_server: str,
        username: str,
        password: Annotated[str, Field(description="Password or app password (write-only)")],
        imap_port: Annotated[
            int | None, Field(description="Default: 993 for ssl, 143 otherwise")
        ] = None,
        imap_security: Security = "ssl",
        character_set: str = "UTF-8",
    ) -> dict[str, Any]:
        """Add an IMAP mail account for Paperless to fetch documents from."""
        body: dict[str, Any] = {
            "name": name,
            "imap_server": imap_server,
            "username": username,
            "password": password,
            "imap_security": _SECURITY[imap_security],
            "character_set": character_set,
        }
        body["imap_port"] = imap_port or (993 if imap_security == "ssl" else 143)
        return _account(await pc.post("mail_accounts", json=body))

    @mcp.tool(annotations=UPDATE)
    async def update_mail_account(
        account_id: int,
        name: str | None = None,
        imap_server: str | None = None,
        imap_port: int | None = None,
        imap_security: Security | None = None,
        username: str | None = None,
        password: str | None = None,
        character_set: str | None = None,
    ) -> dict[str, Any]:
        """Update a mail account. Only the fields you pass change."""
        body: dict[str, Any] = {
            "name": name,
            "imap_server": imap_server,
            "imap_port": imap_port,
            "username": username,
            "password": password,
            "character_set": character_set,
        }
        body = {k: v for k, v in body.items() if v is not None}
        if imap_security:
            body["imap_security"] = _SECURITY[imap_security]
        return _account(await pc.patch(f"mail_accounts/{account_id}", json=body))

    @mcp.tool(annotations=DELETE)
    async def delete_mail_account(account_id: int) -> dict[str, Any]:
        """Delete a mail account and its rules."""
        await pc.delete(f"mail_accounts/{account_id}")
        return {"deleted": account_id}

    @mcp.tool(annotations=WRITE.model_copy(update={"open_world_hint": True}))
    async def process_mail_account(account_id: int) -> dict[str, Any]:
        """Fetch mail for an account now instead of waiting for the schedule."""
        result = await pc.post(f"mail_accounts/{account_id}/process")
        return result if isinstance(result, dict) else {"result": result}

    @mcp.tool(annotations=WRITE)
    async def create_mail_rule(
        name: str,
        account_id: int,
        fields: Annotated[dict[str, Any] | None, Field(description=RULE_FIELDS_HELP)] = None,
    ) -> dict[str, Any]:
        """Create a mail rule for an account."""
        body = {"name": name, "account": account_id} | (fields or {})
        return await pc.post("mail_rules", json=body)

    @mcp.tool(annotations=UPDATE)
    async def update_mail_rule(
        rule_id: int,
        fields: Annotated[dict[str, Any], Field(description=RULE_FIELDS_HELP)],
    ) -> dict[str, Any]:
        """Update a mail rule. Only the fields you pass change."""
        return await pc.patch(f"mail_rules/{rule_id}", json=fields)

    @mcp.tool(annotations=DELETE)
    async def delete_mail_rule(rule_id: int) -> dict[str, Any]:
        """Delete a mail rule."""
        await pc.delete(f"mail_rules/{rule_id}")
        return {"deleted": rule_id}
