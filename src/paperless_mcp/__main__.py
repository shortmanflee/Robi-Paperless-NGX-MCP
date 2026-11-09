"""Entrypoint: `paperless-mcp` or `python -m paperless_mcp`."""

from __future__ import annotations

import logging
import sys

from pydantic import ValidationError

from paperless_mcp.config import load_settings
from paperless_mcp.server import create_server


def main() -> None:
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(f"paperless-mcp: invalid configuration\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the protocol channel in stdio mode
    )
    mcp = create_server(settings)

    if settings.mcp_transport == "stdio":
        mcp.run("stdio", show_banner=False)
        return

    kwargs: dict = {}
    if settings.allowed_hosts:
        kwargs.update(host_origin_protection=True, allowed_hosts=settings.allowed_hosts)
    logging.getLogger(__name__).info(
        "Serving Paperless MCP on http://%s:%s%s (read_only=%s, max upload %.0f MB)",
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
        settings.mcp_read_only,
        settings.mcp_max_upload_mb,
    )
    mcp.run(
        "http",
        show_banner=False,
        host=settings.mcp_host,
        port=settings.mcp_port,
        path=settings.mcp_path,
        log_level=settings.log_level.lower(),
        **kwargs,
    )


if __name__ == "__main__":
    main()
