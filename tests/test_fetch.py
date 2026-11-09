from __future__ import annotations

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.ssrf import ValidatedURL
from httpx import Response

from paperless_mcp import fetch
from paperless_mcp.fetch import FetchError, fetch_to_file, filename_for, filename_from_disposition

PDF = b"%PDF-1.7\n" + b"2" * 3000
PUBLIC_IP = "93.184.216.34"
TASK = "8f6c1c5e-0000-4000-8000-000000000003"


@pytest.fixture
def net(monkeypatch):
    """Fake DNS (every host -> PUBLIC_IP) and a scripted HTTP transport.

    `routes` maps a URL path to a Response or callable; `seen` records
    (validated_url, request) pairs.
    """
    routes: dict[str, object] = {}
    validated: list[str] = []
    requests: list[httpx.Request] = []

    async def fake_validate(url: str, require_path: bool = False) -> ValidatedURL:
        validated.append(url)
        p = httpx.URL(url)
        if p.scheme != "https":
            from fastmcp.server.auth.ssrf import SSRFError

            raise SSRFError(f"URL must use HTTPS, got: {p.scheme}")
        path = p.raw_path.decode()
        return ValidatedURL(url, p.host, p.port or 443, path, [PUBLIC_IP])

    def handler(request: httpx.Request) -> Response:
        requests.append(request)
        route = routes.get(request.url.raw_path.decode())
        if route is None:
            return Response(404)
        return route(request) if callable(route) else route

    monkeypatch.setattr(fetch, "validate_url", fake_validate)
    monkeypatch.setattr(fetch, "_TRANSPORT", httpx.MockTransport(handler))
    return routes, validated, requests


async def test_streams_to_disk_with_pinned_ip(net, tmp_path):
    routes, _validated, requests = net
    routes["/papers/2401.00001"] = Response(
        200, content=PDF, headers={"Content-Type": "application/pdf"}
    )
    dest = tmp_path / "out.part"
    res = await fetch_to_file(
        "https://arxiv.example/papers/2401.00001", dest, max_bytes=10_000, timeout=5
    )
    assert dest.read_bytes() == PDF and res.size == len(PDF)
    assert res.mime == "application/pdf"
    req = requests[0]
    assert req.url.host == PUBLIC_IP  # connected to the validated IP...
    assert req.headers["Host"] == "arxiv.example"  # ...but with the original Host
    assert req.extensions["sni_hostname"] == "arxiv.example"


async def test_redirects_are_revalidated(net, tmp_path):
    routes, validated, _ = net
    routes["/dl"] = Response(302, headers={"Location": "https://cdn.example/f/report.pdf"})
    routes["/f/report.pdf"] = Response(200, content=PDF)
    res = await fetch_to_file("https://site.example/dl", tmp_path / "x", max_bytes=10**6, timeout=5)
    assert validated == ["https://site.example/dl", "https://cdn.example/f/report.pdf"]
    assert res.final_url == "https://cdn.example/f/report.pdf"
    assert res.filename == "report.pdf"


async def test_redirect_to_http_refused(net, tmp_path):
    routes, _, _ = net
    routes["/dl"] = Response(301, headers={"Location": "http://site.example/plain.pdf"})
    with pytest.raises(FetchError, match=r"URL not allowed.*HTTPS"):
        await fetch_to_file("https://site.example/dl", tmp_path / "x", max_bytes=10**6, timeout=5)


async def test_too_many_redirects(net, tmp_path):
    routes, _, _ = net
    routes["/loop"] = Response(302, headers={"Location": "/loop"})
    dest = tmp_path / "x"
    with pytest.raises(FetchError, match="too many redirects"):
        await fetch_to_file("https://site.example/loop", dest, max_bytes=10**6, timeout=5)
    assert not dest.exists()


async def test_size_limit_by_header_and_stream(net, tmp_path):
    routes, _, _ = net
    routes["/declared"] = Response(200, content=PDF)  # httpx sets Content-Length

    def undeclared(_request):
        async def gen():
            for _ in range(10):
                yield b"x" * 1000

        return Response(200, content=gen())

    routes["/streamed"] = undeclared
    dest = tmp_path / "x"
    with pytest.raises(FetchError, match="limit") as e1:
        await fetch_to_file("https://s.example/declared", dest, max_bytes=100, timeout=5)
    with pytest.raises(FetchError, match="exceeds") as e2:
        await fetch_to_file("https://s.example/streamed", dest, max_bytes=5000, timeout=5)
    assert e1.value.status == e2.value.status == 413
    assert not dest.exists()


async def test_http_error_status(net, tmp_path):
    with pytest.raises(FetchError, match="HTTP 404"):
        await fetch_to_file("https://s.example/missing", tmp_path / "x", max_bytes=100, timeout=5)


def test_filenames():
    assert filename_from_disposition('attachment; filename="Q1 report.pdf"') == "Q1 report.pdf"
    assert filename_from_disposition("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf") == (
        "résumé.pdf"
    )
    assert filename_from_disposition('attachment; filename="../../etc/passwd"') == "passwd"
    assert filename_from_disposition(None) is None
    assert filename_for("https://x/a/My%20Doc.pdf?dl=1", "application/pdf") == "My Doc.pdf"
    assert filename_for("https://x/", "application/pdf") == "download.pdf"


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
                     "result_data": {"document_id": 321}}
                ],
            },
        )
    )  # fmt: skip
    return post


async def test_upload_from_url_tool(mcp_client, net, consume, store):
    routes, _, _ = net
    routes["/pdf/2401.00001"] = Response(
        200, content=PDF, headers={"Content-Type": "application/pdf"}
    )
    res = await mcp_client.call_tool(
        "upload_document_from_url",
        {"url": "https://arxiv.example/pdf/2401.00001", "tags": ["Tax"]},
    )
    assert res.data["document_id"] == 321
    assert res.data["source_url"] == "https://arxiv.example/pdf/2401.00001"
    raw = consume.calls[0].request.content
    assert b'filename="2401.00001.pdf"' in raw and PDF in raw
    assert list(store.dir.iterdir()) == []


async def test_upload_from_url_rejects_web_page(mcp_client, net, consume):
    routes, _, _ = net
    routes["/article"] = Response(
        200, content=b"<!doctype html><html>hi</html>", headers={"Content-Type": "text/html"}
    )
    with pytest.raises(ToolError, match="HTML web page"):
        await mcp_client.call_tool(
            "upload_document_from_url", {"url": "https://news.example/article"}
        )
    assert not consume.called


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.com/a.pdf", "HTTPS"),
        ("https://127.0.0.1/a.pdf", "blocked"),
        ("https://192.168.1.178:8000/a.pdf", "blocked"),
        ("https://169.254.169.254/latest/meta-data", "blocked"),
        ("https://[::1]/a.pdf", "blocked"),
    ],
)
async def test_ssrf_guard_blocks_unsafe_urls(mcp_client, url, reason):
    # Real validation (no monkeypatch): these never leave the process.
    with pytest.raises(ToolError, match=f"URL not allowed.*{reason}"):
        await mcp_client.call_tool("upload_document_from_url", {"url": url})


async def test_url_fetch_can_be_disabled(server_factory):
    async with Client(server_factory(mcp_allow_url_fetch=False)) as c:
        names = {t.name for t in await c.list_tools()}
    assert "upload_document_from_url" not in names and "upload_document" in names
