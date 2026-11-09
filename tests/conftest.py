from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from httpx import Response

from paperless_mcp.client import PaperlessClient
from paperless_mcp.config import Settings
from paperless_mcp.server import create_server
from paperless_mcp.uploads import UploadStore

PL_URL = "http://paperless.test"
API = f"{PL_URL}/api"
TOKEN = "t" * 32
PUBLIC_URL = "http://mcp.test"

TAXONOMY: dict[str, list[dict[str, Any]]] = {
    "tags": [
        {"id": 1, "name": "Invoice", "color": "#ff0000", "matching_algorithm": 6},
        {"id": 2, "name": "Inbox", "is_inbox_tag": True, "matching_algorithm": 0},
        {"id": 3, "name": "Tax", "matching_algorithm": 1, "match": "hmrc"},
    ],
    "correspondents": [{"id": 5, "name": "ACME", "matching_algorithm": 6}],
    "document_types": [{"id": 7, "name": "Bill", "matching_algorithm": 6}],
    "storage_paths": [{"id": 9, "name": "Archive", "path": "{{ title }}"}],
    "custom_fields": [
        {"id": 30, "name": "Amount", "data_type": "monetary", "extra_data": {}},
        {
            "id": 40,
            "name": "Status",
            "data_type": "select",
            "extra_data": {
                "select_options": [{"id": "opt1", "label": "Paid"}, {"id": "opt2", "label": "Open"}]
            },
        },
    ],
}


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "paperless_url": PL_URL,
        "paperless_token": "pl-token",
        "mcp_transport": "http",
        "mcp_auth_tokens": TOKEN,
        "mcp_public_url": PUBLIC_URL,
        "_env_file": None,
    }
    return Settings(**(base | overrides))


def body(route: respx.Route, idx: int = -1) -> Any:
    return json.loads(route.calls[idx].request.content)


def _list_handler(kind: str):
    def handler(request: httpx.Request) -> Response:
        rows = TAXONOMY[kind]
        if name := request.url.params.get("name__iexact"):
            rows = [r for r in rows if r["name"].lower() == name.lower()]
        if name := request.url.params.get("name__icontains"):
            rows = [r for r in rows if name.lower() in r["name"].lower()]
        return Response(200, json={"count": len(rows), "next": None, "results": rows})

    return handler


@pytest.fixture
def pl_mock() -> respx.MockRouter:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        for kind in TAXONOMY:
            router.get(f"/{kind}/", name=f"list_{kind}").mock(side_effect=_list_handler(kind))
            for row in TAXONOMY[kind]:
                router.get(f"/{kind}/{row['id']}/").mock(return_value=Response(200, json=row))
        router.get("/ui_settings/", name="ui_settings").mock(
            return_value=Response(
                200, json={"user": {"id": 1}}, headers={"X-Version": "3.4.2", "X-Api-Version": "10"}
            )
        )
        yield router


@pytest.fixture
def store(tmp_path: Path) -> UploadStore:
    return UploadStore(tmp_path / "staging", ttl=900, max_bytes=1024 * 1024, max_concurrent=2)


@pytest.fixture
def server_factory(pl_mock: respx.MockRouter, store: UploadStore) -> Callable[..., FastMCP]:
    def factory(**overrides: Any) -> FastMCP:
        settings = make_settings(mcp_upload_dir=store.dir, **overrides)
        pc = PaperlessClient(PL_URL, token="pl-token")
        return create_server(settings, pc, store)

    return factory


@pytest.fixture
async def mcp_client(server_factory) -> AsyncIterator[Client]:
    async with Client(server_factory()) as client:
        yield client


def document(**kw: Any) -> dict[str, Any]:
    """A realistic Document payload (API v10)."""
    base = {
        "id": 101,
        "title": "Electricity bill March",
        "content": "ACME Energy\nAmount due: 120.50\n" + "x" * 50,
        "correspondent": 5,
        "document_type": 7,
        "storage_path": None,
        "tags": [1, 2],
        "created": "2025-03-04",
        "created_date": "2025-03-04",
        "added": "2025-03-05T10:00:00Z",
        "modified": "2025-03-05T10:00:00Z",
        "archive_serial_number": 12,
        "original_file_name": "bill.pdf",
        "archived_file_name": "2025-03-04 Electricity bill March.pdf",
        "custom_fields": [{"field": 30, "value": "GBP120.50"}],
        "notes": [],
        "page_count": 2,
        "mime_type": "application/pdf",
        "owner": 1,
        "duplicate_documents": [],
        "versions": [],
    }
    return base | kw
