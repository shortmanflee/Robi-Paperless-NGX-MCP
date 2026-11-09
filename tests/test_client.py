from __future__ import annotations

import base64

import httpx
import pytest
import respx
from httpx import Response

from paperless_mcp.client import PaperlessClient, PaperlessError, api_path

from .conftest import API, PL_URL


@pytest.fixture
async def pc():
    client = PaperlessClient(PL_URL, token="abc")
    yield client
    await client.aclose()


def test_api_path_adds_trailing_slash():
    assert api_path("documents/1") == "/documents/1/"
    assert api_path("/tags/") == "/tags/"


async def test_sends_token_and_api_version(pc, pl_mock):
    route = pl_mock.get("/documents/5/").mock(return_value=Response(200, json={"id": 5}))
    await pc.get("documents/5")
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Token abc"
    assert req.headers["Accept"] == "application/json; version=10"


async def test_basic_auth_and_token_prefix_stripped(pl_mock):
    route = pl_mock.get("/documents/5/").mock(return_value=Response(200, json={}))
    await PaperlessClient(PL_URL, username="u", password="p").get("documents/5")
    await PaperlessClient(PL_URL, token="Token xyz").get("documents/5")
    assert route.calls[0].request.headers["Authorization"] == (
        "Basic " + base64.b64encode(b"u:p").decode()
    )
    assert route.calls[1].request.headers["Authorization"] == "Token xyz"


async def test_drf_errors_are_flattened(pc, pl_mock):
    pl_mock.post("/tags/").mock(
        return_value=Response(400, json={"name": ["Tag with this name already exists."]})
    )
    with pytest.raises(PaperlessError, match=r"400: name: Tag with this name already exists"):
        await pc.post("tags", json={"name": "x"})


async def test_html_error_pages_are_not_dumped(pc, pl_mock):
    pl_mock.get("/documents/1/").mock(return_value=Response(502, text="<html>Bad gateway</html>"))
    with pytest.raises(PaperlessError) as exc:
        await pc.get("documents/1")
    assert "<html>" not in str(exc.value)


async def test_unreachable(pl_mock):
    with respx.mock() as router:
        router.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(PaperlessError, match="unreachable"):
            await PaperlessClient("http://nowhere.test", token="x").get("tags")


async def test_paginate_follows_next(pc, pl_mock):
    def handler(request):
        page = int(request.url.params["page"])
        nxt = f"{API}/documents/?page={page + 1}" if page < 3 else None
        return Response(200, json={"count": 3, "next": nxt, "results": [{"id": page}]})

    pl_mock.get("/documents/").mock(side_effect=handler)
    assert [d["id"] for d in await pc.paginate("documents")] == [1, 2, 3]
    assert [d["id"] for d in await pc.paginate("documents", limit=2)] == [1, 2]


async def test_resolve_ids_by_id_or_name(pc, pl_mock):
    assert await pc.resolve_ids("tags", [1, "3", "invoice", "TAX"]) == [1, 3, 1, 3]
    with pytest.raises(PaperlessError, match="no tag named 'Nope'"):
        await pc.resolve_id("tags", "Nope")
    assert await pc.resolve_optional("correspondents", "") == 0
    assert await pc.resolve_optional("correspondents", None) is None
    assert await pc.resolve_optional("correspondents", "acme") == 5


async def test_names_are_cached_and_invalidated(pc, pl_mock):
    assert (await pc.names("tags"))[1] == "Invoice"
    await pc.names("tags")
    assert pl_mock.routes["list_tags"].call_count == 1
    pc.invalidate("tags")
    await pc.names("tags")
    assert pl_mock.routes["list_tags"].call_count == 2


async def test_records_server_version(pc, pl_mock):
    await pc.get("ui_settings")
    assert pc.server_version == "3.4.2"
    assert pc.server_api_version == "10"
