from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

from .conftest import body, document


async def test_read_only_mode_hides_write_tools(server_factory):
    async with Client(server_factory(mcp_read_only=True)) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"search_documents", "get_document", "create_download_link"} <= names
    writers = ("create_", "update_", "delete_", "upload_", "bulk_", "merge_", "empty_", "add_")
    leaked = {n for n in names if n.startswith(writers)} - {"create_download_link"}
    assert not leaked


async def test_annotations(mcp_client):
    tools = {t.name: t for t in await mcp_client.list_tools()}
    assert tools["get_document"].annotations.read_only_hint is True
    for name in ("delete_document", "empty_trash", "merge_documents", "delete_pages"):
        assert tools[name].annotations.destructive_hint is True, name
    assert tools["upload_document_from_url"].annotations.open_world_hint is True


async def test_tool_search_mode(server_factory):
    async with Client(server_factory(mcp_tool_search="bm25")) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"search_documents", "get_document", "search_tools", "call_tool"} <= names
    assert "delete_document" not in names


# ------------------------------------------------------------------ documents
async def test_search_maps_filters_and_names(mcp_client, pl_mock):
    route = pl_mock.get("/documents/").mock(
        return_value=Response(
            200,
            json={
                "count": 1,
                "next": None,
                "results": [
                    document()
                    | {"__search_hit__": {"score": 0.91234, "highlights": "<b>bill</b>", "rank": 1}}
                ],
            },
        )
    )
    res = await mcp_client.call_tool(
        "search_documents",
        {
            "text": "bill",
            "tags": ["invoice", 3],
            "exclude_tags": ["Inbox"],
            "correspondent": "acme",
            "created_from": "2025-01-01",
        },
    )
    q = route.calls[0].request.url.params
    assert q["text"] == "bill"
    assert q["tags__id__all"] == "1,3"
    assert q["tags__id__none"] == "2"
    assert q["correspondent__id"] == "5"
    assert q["created__date__gte"] == "2025-01-01"
    assert "ordering" not in q  # relevance when searching
    assert q["truncate_content"] == "true"
    data = res.data
    assert data["count"] == 1 and data["has_more"] is False
    assert data["results"][0] == {
        "id": 101,
        "title": "Electricity bill March",
        "created": "2025-03-04",
        "correspondent": "ACME",
        "document_type": "Bill",
        "tags": ["Invoice", "Inbox"],
        "asn": 12,
        "page_count": 2,
        "score": 0.912,
        "highlights": "<b>bill</b>",
    }


async def test_search_without_text_orders_newest_first(mcp_client, pl_mock):
    route = pl_mock.get("/documents/").mock(
        return_value=Response(200, json={"count": 0, "next": None, "results": []})
    )
    await mcp_client.call_tool("search_documents", {"title_contains": "x"})
    assert route.calls[0].request.url.params["ordering"] == "-created"


async def test_get_document_pages_content(mcp_client, pl_mock):
    pl_mock.get("/documents/101/").mock(return_value=Response(200, json=document()))
    res = await mcp_client.call_tool(
        "get_document", {"document_id": 101, "include_content": True, "content_limit": 10}
    )
    d = res.data
    assert d["content"] == "ACME Energ"
    assert d["content_next_offset"] == 10
    assert d["content_length"] > 10
    assert d["correspondent"] == {"id": 5, "name": "ACME"}
    assert d["custom_fields"] == {"Amount": "GBP120.50"}
    assert "storage_path" not in d

    res = await mcp_client.call_tool("get_document", {"document_id": 101})
    assert "content" not in res.data


async def test_update_document_merges_tags_and_custom_fields(mcp_client, pl_mock):
    pl_mock.get("/documents/101/").mock(return_value=Response(200, json=document()))
    patch = pl_mock.patch("/documents/101/").mock(return_value=Response(200, json=document()))
    await mcp_client.call_tool(
        "update_document",
        {
            "document_id": 101,
            "add_tags": ["Tax"],
            "remove_tags": ["inbox"],
            "correspondent": "",
            "custom_fields": {"Status": "paid", "Amount": None},
        },
    )
    assert body(patch) == {
        "correspondent": None,
        "tags": [1, 3],
        "custom_fields": [{"field": 40, "value": "opt1"}],
    }


async def test_update_document_rejects_bad_select_option(mcp_client, pl_mock):
    pl_mock.get("/documents/101/").mock(return_value=Response(200, json=document()))
    with pytest.raises(ToolError, match="not an option of 'Status'"):
        await mcp_client.call_tool(
            "update_document", {"document_id": 101, "custom_fields": {"Status": "Maybe"}}
        )


async def test_update_document_requires_a_change(mcp_client):
    with pytest.raises(ToolError, match="nothing to update"):
        await mcp_client.call_tool("update_document", {"document_id": 101})


async def test_delete_and_trash(mcp_client, pl_mock):
    delete = pl_mock.delete("/documents/101/").mock(return_value=Response(204))
    trash = pl_mock.post("/trash/").mock(
        return_value=Response(200, json={"result": "OK", "doc_ids": [101]})
    )
    assert (await mcp_client.call_tool("delete_document", {"document_id": 101})).data == {
        "trashed": 101
    }
    assert delete.called
    await mcp_client.call_tool("restore_documents", {"document_ids": [101]})
    assert body(trash) == {"action": "restore", "documents": [101]}
    res = await mcp_client.call_tool("empty_trash", {})
    assert body(trash) == {"action": "empty"}
    assert res.data == {"emptied": [101]}


async def test_download_too_large_points_to_link(server_factory, pl_mock):
    pl_mock.get("/documents/101/download/").mock(
        return_value=Response(200, content=b"%PDF-" + b"0" * 2000)
    )
    async with Client(server_factory(mcp_max_inline_mb=0.001)) as c:
        with pytest.raises(ToolError, match="create_download_link"):
            await c.call_tool("download_document", {"document_id": 101})


async def test_download_inline_pdf(mcp_client, pl_mock):
    route = pl_mock.get("/documents/101/download/").mock(
        return_value=Response(
            200, content=b"%PDF-1.7 tiny", headers={"Content-Type": "application/pdf"}
        )
    )
    res = await mcp_client.call_tool("download_document", {"document_id": 101, "original": True})
    assert route.calls[0].request.url.params["original"] == "true"
    assert res.content[0].type == "resource"
    assert res.content[0].resource.mime_type == "application/pdf"


async def test_notes(mcp_client, pl_mock):
    add = pl_mock.post("/documents/101/notes/").mock(
        return_value=Response(200, json=[{"id": 1, "note": "hi", "user": {"username": "me"}}])
    )
    delete = pl_mock.delete("/documents/101/notes/").mock(return_value=Response(200, json=[]))
    res = await mcp_client.call_tool("add_note", {"document_id": 101, "note": "hi"})
    assert body(add) == {"note": "hi"}
    assert res.data == [{"id": 1, "note": "hi", "user": "me"}]
    await mcp_client.call_tool("delete_note", {"document_id": 101, "note_id": 1})
    assert delete.calls[0].request.url.params["id"] == "1"


# ------------------------------------------------------------------- bulk & pdf
async def test_bulk_edit_issues_one_call_per_change(mcp_client, pl_mock):
    route = pl_mock.post("/documents/bulk_edit/").mock(
        return_value=Response(200, json={"result": "OK"})
    )
    res = await mcp_client.call_tool(
        "bulk_edit_documents",
        {
            "document_ids": [1, 2],
            "add_tags": ["Tax"],
            "document_type": "bill",
            "storage_path": "",
            "set_custom_fields": {"Amount": "EUR5.00"},
        },
    )
    calls = [body(route, i) for i in range(route.call_count)]
    assert calls == [
        {
            "documents": [1, 2],
            "method": "modify_tags",
            "parameters": {"add_tags": [3], "remove_tags": []},
        },
        {"documents": [1, 2], "method": "set_document_type", "parameters": {"document_type": 7}},
        {"documents": [1, 2], "method": "set_storage_path", "parameters": {"storage_path": None}},
        {
            "documents": [1, 2],
            "method": "modify_custom_fields",
            "parameters": {"add_custom_fields": {"30": "EUR5.00"}, "remove_custom_fields": []},
        },
    ]
    assert res.data["applied"] == ["tags", "document_type", "storage_path", "custom_fields"]


async def test_split_document_builds_edit_pdf_operations(mcp_client, pl_mock):
    pl_mock.get("/documents/101/").mock(return_value=Response(200, json=document(page_count=5)))
    route = pl_mock.post("/documents/edit_pdf/").mock(
        return_value=Response(200, json={"result": "OK"})
    )
    await mcp_client.call_tool("split_document", {"document_id": 101, "split_after": [2]})
    sent = body(route)
    assert sent["documents"] == [101]
    assert sent["operations"] == [
        {"page": 1, "doc": 0},
        {"page": 2, "doc": 0},
        {"page": 3, "doc": 1},
        {"page": 4, "doc": 1},
        {"page": 5, "doc": 1},
    ]
    assert sent["update_document"] is False


async def test_delete_pages_keeps_the_rest(mcp_client, pl_mock):
    pl_mock.get("/documents/101/").mock(return_value=Response(200, json=document(page_count=4)))
    route = pl_mock.post("/documents/edit_pdf/").mock(
        return_value=Response(200, json={"result": "OK"})
    )
    await mcp_client.call_tool("delete_pages", {"document_id": 101, "pages": [2, 4]})
    assert body(route)["operations"] == [{"page": 1}, {"page": 3}]
    assert body(route)["update_document"] is True
    with pytest.raises(ToolError, match="cannot remove every page"):
        await mcp_client.call_tool("delete_pages", {"document_id": 101, "pages": [1, 2, 3, 4]})


async def test_merge(mcp_client, pl_mock):
    route = pl_mock.post("/documents/merge/").mock(
        return_value=Response(200, json={"result": "OK"})
    )
    await mcp_client.call_tool(
        "merge_documents", {"document_ids": [3, 1], "metadata_document_id": 3}
    )
    assert body(route) == {
        "documents": [3, 1],
        "delete_originals": False,
        "metadata_document_id": 3,
    }


# ------------------------------------------------------------------- taxonomy
async def test_list_tags_shapes_matching(mcp_client):
    res = await mcp_client.call_tool("list_tags", {})
    tags = {t["name"]: t for t in res.data["results"]}
    assert tags["Invoice"]["matching_algorithm"] == "auto"
    assert tags["Tax"] == {
        "id": 3,
        "name": "Tax",
        "match": "hmrc",
        "matching_algorithm": "any",
    }


async def test_get_by_name(mcp_client):
    res = await mcp_client.call_tool("get_correspondent", {"ref": "acme"})
    assert res.data["id"] == 5


async def test_create_and_update_tag(mcp_client, pl_mock):
    create = pl_mock.post("/tags/").mock(return_value=Response(201, json={"id": 8, "name": "Car"}))
    patch = pl_mock.patch("/tags/8/").mock(return_value=Response(200, json={"id": 8}))
    await mcp_client.call_tool(
        "create_tag",
        {"name": "Car", "color": "#00ff00", "parent": "Tax", "matching_algorithm": "literal"},
    )
    assert body(create) == {"name": "Car", "color": "#00ff00", "parent": 3, "matching_algorithm": 3}
    await mcp_client.call_tool("update_tag", {"tag": 8, "parent": ""})
    assert body(patch) == {"parent": None}


async def test_writes_invalidate_name_cache(mcp_client, pl_mock):
    pl_mock.post("/correspondents/").mock(return_value=Response(201, json={"id": 6, "name": "B"}))
    pl_mock.get("/documents/").mock(
        return_value=Response(200, json={"count": 0, "next": None, "results": []})
    )
    await mcp_client.call_tool("search_documents", {})
    await mcp_client.call_tool("create_correspondent", {"name": "B"})
    await mcp_client.call_tool("search_documents", {})
    assert pl_mock.routes["list_correspondents"].call_count == 2


async def test_delete_storage_path_by_name(mcp_client, pl_mock):
    route = pl_mock.delete("/storage_paths/9/").mock(return_value=Response(204))
    res = await mcp_client.call_tool("delete_storage_path", {"ref": "archive"})
    assert route.called and res.data == {"deleted": 9}


async def test_update_custom_field_keeps_option_ids(mcp_client, pl_mock):
    patch = pl_mock.patch("/custom_fields/40/").mock(
        return_value=Response(200, json={"id": 40, "name": "Status"})
    )
    await mcp_client.call_tool(
        "update_custom_field", {"field": "Status", "select_options": ["Open", "Overdue"]}
    )
    assert body(patch) == {
        "extra_data": {
            "select_options": [{"label": "Open", "id": "opt2"}, {"label": "Overdue", "id": None}]
        }
    }


# --------------------------------------------------------------------- misc
async def test_share_link_url_and_expiry(mcp_client, pl_mock):
    route = pl_mock.post("/share_links/").mock(
        return_value=Response(
            201, json={"id": 1, "slug": "abc", "document": 101, "file_version": "archive"}
        )
    )
    res = await mcp_client.call_tool("create_share_link", {"document_id": 101})
    assert res.data["url"] == "http://paperless.test/share/abc"
    assert "expiration" in body(route)


def test_share_links_use_paperless_public_url():
    from paperless_mcp.server import build_client

    from .conftest import make_settings

    assert build_client(make_settings()).public_url == "http://paperless.test"
    pc = build_client(make_settings(paperless_public_url="https://pl.lan/"))
    assert pc.public_url == "https://pl.lan" and pc.base_url == "http://paperless.test"


async def test_mail_account_port_defaults(mcp_client, pl_mock):
    route = pl_mock.post("/mail_accounts/").mock(return_value=Response(201, json={"id": 1}))
    await mcp_client.call_tool(
        "create_mail_account",
        {"name": "m", "imap_server": "imap.x", "username": "u", "password": "p"},
    )
    assert body(route)["imap_port"] == 993 and body(route)["imap_security"] == 2
    await mcp_client.call_tool(
        "create_mail_account",
        {"name": "m", "imap_server": "imap.x", "username": "u", "password": "p",
         "imap_security": "starttls"},
    )  # fmt: skip
    assert body(route)["imap_port"] == 143


async def test_mail_account_password_never_returned(mcp_client, pl_mock):
    pl_mock.get("/mail_accounts/").mock(
        return_value=Response(
            200,
            json={
                "count": 1,
                "next": None,
                "results": [{"id": 1, "name": "m", "password": "**********", "imap_security": 2}],
            },
        )
    )
    res = await mcp_client.call_tool("list_mail_accounts", {})
    assert res.data == [{"id": 1, "name": "m", "imap_security": "ssl"}]


async def test_server_status_hides_urls(mcp_client, pl_mock):
    pl_mock.get("/status/").mock(
        return_value=Response(
            200,
            json={
                "database": {"type": "sqlite", "url": "/data/db.sqlite3", "status": "OK"},
                "tasks": {"redis_url": "redis://:secret@redis", "redis_status": "OK"},
            },
        )
    )
    res = (await mcp_client.call_tool("get_server_status", {})).data
    assert res["paperless_version"] == "3.4.2"
    assert res["tasks"] == {"redis_status": "OK"}
    assert "url" not in res["database"]


async def test_resources(mcp_client):
    res = await mcp_client.read_resource("paperless://document-types")
    assert '"Bill"' in res[0].text
