from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from paperless_mcp.auth import StaticBearerVerifier
from paperless_mcp.config import load_settings

from .conftest import TOKEN, make_settings

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


async def test_verifier_accepts_any_configured_token():
    v = StaticBearerVerifier(["a" * 20, "b" * 20])
    assert (await v.verify_token("b" * 20)).client_id == "lan-client-1"
    assert await v.verify_token("c" * 20) is None
    assert await v.verify_token("") is None


@asynccontextmanager
async def lan_http(server_factory, **overrides):
    app = server_factory(**overrides).http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://192.168.1.50") as c:
            yield c


async def test_http_rejects_missing_or_wrong_token(server_factory):
    async with lan_http(server_factory) as http:
        r1 = await http.post("/mcp", json=INIT, headers=MCP_HEADERS)
        r2 = await http.post(
            "/mcp",
            json=INIT,
            headers=MCP_HEADERS | {"Authorization": "Bearer wrong-token-xxxxxxxx"},
        )
    assert r1.status_code == 401
    assert r2.status_code == 401


async def test_http_accepts_valid_token(server_factory):
    async with lan_http(server_factory) as http:
        r = await http.post(
            "/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": f"Bearer {TOKEN}"}
        )
    assert r.status_code == 200
    assert "Paperless-NGX" in r.text


async def test_health_is_public(server_factory):
    async with lan_http(server_factory) as http:
        r = await http.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "paperless": "ok"}


async def test_health_reports_unreachable(server_factory, pl_mock):
    pl_mock.routes["ui_settings"].mock(
        return_value=httpx.Response(401, json={"detail": "Invalid token."})
    )
    async with lan_http(server_factory) as http:
        r = await http.get("/health")
    assert r.json() == {"status": "ok", "paperless": "unreachable", "detail": "Invalid token."}


def test_http_requires_tokens():
    with pytest.raises(ValidationError, match="MCP_AUTH_TOKENS is required"):
        make_settings(mcp_auth_tokens=None)


def test_short_tokens_rejected():
    with pytest.raises(ValidationError, match="at least 16"):
        make_settings(mcp_auth_tokens="short")


def test_stdio_needs_no_tokens():
    assert make_settings(mcp_transport="stdio", mcp_auth_tokens=None).auth_tokens == []


def test_requires_paperless_credentials():
    with pytest.raises(ValidationError, match="PAPERLESS_TOKEN"):
        make_settings(paperless_token=None)
    s = make_settings(paperless_token="", paperless_username="u", paperless_password="p")
    assert s.paperless_username == "u" and s.paperless_token is None
    with pytest.raises(ValidationError, match="PAPERLESS_TOKEN"):
        make_settings(paperless_token="  ")


def test_urls_normalised():
    s = make_settings(paperless_url="http://pl:8000/api/", mcp_public_url="https://mcp.lan/")
    assert s.paperless_url == "http://pl:8000"
    assert s.mcp_public_url == "https://mcp.lan"
    with pytest.raises(ValidationError):
        make_settings(mcp_public_url="mcp.lan")


def test_size_settings():
    s = make_settings(mcp_max_upload_mb=100, mcp_max_inline_mb=10)
    assert s.max_upload_bytes == 100 * 1024 * 1024
    assert s.max_inline_bytes == 10 * 1024 * 1024


def test_local_roots(tmp_path):
    s = make_settings(mcp_local_upload_roots=f"{tmp_path}, ~/Documents")
    assert s.local_upload_roots[0] == tmp_path.resolve()
    assert s.local_upload_roots[1].name == "Documents"


def test_secret_files(tmp_path, monkeypatch):
    tokens = tmp_path / "tokens"
    tokens.write_text("x" * 20 + ",y" + "y" * 19 + "\n")
    pl_token = tmp_path / "pl"
    pl_token.write_text("secret-pl-token\n")
    for k, v in {
        "PAPERLESS_URL": "http://pl",
        "PAPERLESS_TOKEN_FILE": str(pl_token),
        "MCP_AUTH_TOKENS_FILE": str(tokens),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
    monkeypatch.delenv("PAPERLESS_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # avoid picking up a developer .env
    s = load_settings()
    assert s.auth_tokens == ["x" * 20, "y" * 20]
    assert s.paperless_token.get_secret_value() == "secret-pl-token"
