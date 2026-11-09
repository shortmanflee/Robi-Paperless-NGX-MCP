"""Streaming, SSRF-safe download of a public HTTPS URL straight to disk.

FastMCP's `ssrf_safe_fetch_response` buffers the body in memory and caps the whole fetch
at a minute, which is fine for small images but not for 100 MB PDFs. This module reuses
its URL validation (HTTPS only, private/loopback/link-local/metadata IPs refused, DNS
resolved once and pinned) but streams the body to a file, re-validating every redirect.
"""

from __future__ import annotations

import asyncio
import mimetypes
import posixpath
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from fastmcp.server.auth.ssrf import SSRFError, ValidatedURL, format_ip_for_url, validate_url

from paperless_mcp import __version__

REDIRECTS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 30.0
USER_AGENT = f"paperless-mcp/{__version__} (document fetch)"

# Tests swap this for an httpx.MockTransport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class FetchError(Exception):
    """Download failed. `status` mirrors the HTTP status we report to the client."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


@dataclass
class FetchResult:
    path: Path
    size: int
    mime: str
    final_url: str
    filename: str


@dataclass
class _Target:
    url: str
    host_header: str | None
    sni_hostname: str | None
    proxy_url: str | None = None


def _targets(v: ValidatedURL) -> list[_Target]:
    """One pinned IP-literal target per resolved address; proxy mode dials the original URL."""
    if not v.resolved_ips:
        return [_Target(v.original_url, None, None, v.proxy_url)]
    return [
        _Target(
            url=f"https://{format_ip_for_url(ip)}:{v.port}{v.path}",
            host_header=v.hostname,
            sni_hostname=v.hostname,
        )
        for ip in v.resolved_ips
    ]


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    msg = Message()
    msg["content-disposition"] = value
    name = msg.get_filename()
    if not name:
        return None
    return posixpath.basename(name.replace("\\", "/")) or None


def filename_for(url: str, mime: str) -> str:
    name = posixpath.basename(unquote(urlparse(url).path)) or "download"
    if not posixpath.splitext(name)[1]:
        name += mimetypes.guess_extension(mime) or ""
    return name[:200]


async def _stream_once(
    target: _Target, dest: Path, max_bytes: int
) -> tuple[int, httpx.Headers] | tuple[None, httpx.Headers]:
    """GET one target. Returns (bytes_written, headers) on 200 or (None, headers) on redirect."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if target.host_header:
        headers["Host"] = target.host_header
    extensions = {"sni_hostname": target.sni_hostname} if target.sni_hostname else {}
    timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
    async with (
        httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            verify=True,
            proxy=target.proxy_url,
            trust_env=target.proxy_url is None,
            transport=_TRANSPORT,
        ) as client,
        client.stream("GET", target.url, headers=headers, extensions=extensions) as resp,
    ):
        if resp.status_code in REDIRECTS:
            return None, resp.headers
        if resp.status_code != 200:
            raise FetchError(
                502, f"HTTP {resp.status_code} from {target.host_header or target.url}"
            )
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise FetchError(
                413,
                f"file is {_mb(int(declared))}; the limit is {_mb(max_bytes)} (MCP_MAX_UPLOAD_MB)",
            )
        written = 0
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(1024 * 256):
                written += len(chunk)
                if written > max_bytes:
                    raise FetchError(
                        413, f"file exceeds the {_mb(max_bytes)} limit (MCP_MAX_UPLOAD_MB)"
                    )
                fh.write(chunk)
        return written, resp.headers


async def _fetch(url: str, dest: Path, max_bytes: int) -> FetchResult:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            validated = await validate_url(current)
        except SSRFError as exc:
            raise FetchError(400, f"URL not allowed: {exc}") from exc
        last_error: Exception | None = None
        for target in _targets(validated):
            try:
                written, headers = await _stream_once(target, dest, max_bytes)
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                continue
        else:
            raise FetchError(502, f"could not download {current}: {last_error}")

        if written is None:
            location = headers.get("location")
            if not location:
                raise FetchError(502, f"redirect without Location from {current}")
            current = urljoin(current, location)
            continue

        mime = (headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        name = filename_from_disposition(headers.get("content-disposition")) or filename_for(
            current, mime
        )
        return FetchResult(dest, written, mime.lower(), current, name[:200])
    raise FetchError(502, f"too many redirects (> {MAX_REDIRECTS})")


async def fetch_to_file(url: str, dest: Path, *, max_bytes: int, timeout: float) -> FetchResult:
    """Download `url` into `dest`. Deletes `dest` on failure. Raises FetchError."""
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(url, dest, max_bytes)
    except TimeoutError as exc:
        dest.unlink(missing_ok=True)
        raise FetchError(504, f"download took longer than {timeout:.0f}s") from exc
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
