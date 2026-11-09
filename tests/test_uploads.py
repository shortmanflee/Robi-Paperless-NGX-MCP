from __future__ import annotations

import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

from .conftest import PUBLIC_URL, document

PDF = b"%PDF-1.7\n" + b"1" * 5000
TASK = "8f6c1c5e-0000-4000-8000-000000000002"


@asynccontextmanager
async def mcp_and_http(server_factory, **overrides):
    """Both an MCP client (to create links) and an HTTP client (to use them) on one server."""
    mcp = server_factory(**overrides)
    app = mcp.http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http,
            Client(mcp) as client,
        ):
            yield client, http


def path_of(url: str) -> str:
    assert url.startswith(PUBLIC_URL)
    return url[len(PUBLIC_URL) :]


@pytest.fixture
def consume(pl_mock):
    post = pl_mock.post("/documents/post_document/").mock(return_value=Response(200, json=TASK))
    pl_mock.get("/tasks/").mock(
        return_value=Response(
            200,
            json={
                "count": 1,
                "next": None,
                "results": [
                    {"id": 1, "task_id": TASK, "status": "success",
                     "result_data": {"document_id": 900}}
                ],
            },
        )
    )  # fmt: skip
    return post


async def test_upload_link_round_trip(server_factory, consume, store):
    async with mcp_and_http(server_factory) as (client, http):
        link = (
            await client.call_tool(
                "create_upload_link",
                {
                    "filename": "big scan.pdf",
                    "tags": ["Invoice"],
                    "title": "Scan",
                    "wait_seconds": 5,
                },
            )
        ).data
        assert link["curl_command"] == f"curl -fsS -T 'big scan.pdf' {link['upload_url']}"
        assert link["max_size"] == "1.0 MB"

        r = await http.put(path_of(link["upload_url"]), content=PDF)
        assert r.status_code == 200, r.text
        assert r.json()["document_id"] == 900
        raw = consume.calls[0].request.content
        assert PDF in raw and b'filename="big scan.pdf"' in raw
        assert b'name="title"\r\n\r\nScan\r\n' in raw and b'name="tags"\r\n\r\n1\r\n' in raw

        status = (await client.call_tool("get_link_status", {"slot_id": link["slot_id"]})).data
        assert status["state"] == "done" and status["result"]["document_id"] == 900

        again = await http.put(path_of(link["upload_url"]), content=PDF)
        assert again.status_code == 404  # single use
    assert list(store.dir.iterdir()) == []


async def test_upload_streams_chunked_body(server_factory, consume):
    async def chunks():
        for i in range(0, len(PDF), 1000):
            yield PDF[i : i + 1000]

    async with mcp_and_http(server_factory) as (client, http):
        link = (await client.call_tool("create_upload_link", {"filename": "a.pdf"})).data
        r = await http.put(path_of(link["upload_url"]), content=chunks())
    assert r.status_code == 200
    assert PDF in consume.calls[0].request.content


async def test_create_link_validates_metadata_first(server_factory):
    async with mcp_and_http(server_factory) as (client, _):
        with pytest.raises(ToolError, match="no document type named 'Nope'"):
            await client.call_tool(
                "create_upload_link", {"filename": "a.pdf", "document_type": "Nope"}
            )


async def test_upload_too_large(server_factory, consume, store):
    big = b"%PDF-" + b"0" * (store.max_bytes + 1)

    async def stream():  # no Content-Length: enforced while streaming
        yield big

    async with mcp_and_http(server_factory) as (client, http):
        link = (await client.call_tool("create_upload_link", {"filename": "a.pdf"})).data
        url = path_of(link["upload_url"])
        r1 = await http.put(url, content=big)  # Content-Length checked up front
        assert r1.status_code == 413
        r2 = await http.put(url, content=stream())  # slot still usable after an up-front reject
        assert r2.status_code == 413
        status = (await client.call_tool("get_link_status", {"slot_id": link["slot_id"]})).data
        assert status["state"] == "failed"
    assert not consume.called
    assert list(store.dir.iterdir()) == []


async def test_upload_rejects_unsupported_file(server_factory, consume):
    async with mcp_and_http(server_factory) as (client, http):
        link = (await client.call_tool("create_upload_link", {"filename": "a.zip"})).data
        r = await http.put(path_of(link["upload_url"]), content=b"PK\x03\x04zipzip")
    assert r.status_code == 415
    assert not consume.called


async def test_expired_and_unknown_links(server_factory, store):
    async with mcp_and_http(server_factory) as (client, http):
        link = (await client.call_tool("create_upload_link", {"filename": "a.pdf"})).data
        store.get(link["slot_id"]).expires_at = time.time() - 1
        assert (await http.put(path_of(link["upload_url"]), content=PDF)).status_code == 404
        assert (await http.put("/uploads/not-a-token", content=PDF)).status_code == 404
        with pytest.raises(ToolError, match="unknown or expired"):
            await client.call_tool("get_link_status", {"slot_id": link["slot_id"]})


async def test_concurrency_limit(server_factory, store, consume):
    async with mcp_and_http(server_factory) as (client, http):
        link = (await client.call_tool("create_upload_link", {"filename": "a.pdf"})).data
        store._active = store._max_concurrent
        r = await http.put(path_of(link["upload_url"]), content=PDF)
        assert r.status_code == 429
        store._active = 0
        r = await http.put(path_of(link["upload_url"]), content=PDF)  # retry works
        assert r.status_code == 200


async def test_upload_route_absent_in_read_only(server_factory):
    async with mcp_and_http(server_factory, mcp_read_only=True) as (client, http):
        names = {t.name for t in await client.list_tools()}
        assert "create_upload_link" not in names
        r = await http.put("/uploads/whatever", content=PDF)
        assert r.status_code in (404, 405)


async def test_download_link_streams_once(server_factory, pl_mock):
    pl_mock.get("/documents/101/").mock(return_value=Response(200, json=document()))
    dl = pl_mock.get("/documents/101/download/").mock(
        return_value=Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    )
    async with mcp_and_http(server_factory, mcp_read_only=True) as (client, http):
        link = (
            await client.call_tool("create_download_link", {"document_id": 101, "original": True})
        ).data
        assert link["curl_command"].startswith("curl -fL -o bill.pdf ")
        r = await http.get(path_of(link["download_url"]))
        assert r.status_code == 200
        assert r.content == PDF
        assert r.headers["content-type"] == "application/pdf"
        assert "bill.pdf" in r.headers["content-disposition"]
        assert dl.calls[0].request.url.params["original"] == "true"
        assert (await http.get(path_of(link["download_url"]))).status_code == 404
        status = (await client.call_tool("get_link_status", {"slot_id": link["slot_id"]})).data
        assert status["state"] == "done" and status["result"]["size_bytes"] == len(PDF)


async def test_download_link_upstream_error(server_factory, pl_mock):
    pl_mock.get("/documents/5/").mock(return_value=Response(200, json=document(id=5)))
    pl_mock.get("/documents/5/download/").mock(return_value=Response(404, json={"detail": "x"}))
    async with mcp_and_http(server_factory) as (client, http):
        link = (await client.call_tool("create_download_link", {"document_id": 5})).data
        r = await http.get(path_of(link["download_url"]))
    assert r.status_code == 404


async def test_link_base_falls_back_to_request(server_factory, pl_mock):
    """Without MCP_PUBLIC_URL, links use the base URL the client connected to."""
    from paperless_mcp.uploads import link_base

    with pytest.raises(ToolError, match="MCP_PUBLIC_URL"):
        link_base(type("S", (), {"mcp_public_url": None})())
