"""Runtime configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secrets that may alternatively be supplied as `<NAME>_FILE=/run/secrets/...` (Docker secrets).
_FILE_SECRETS = ("PAPERLESS_TOKEN", "PAPERLESS_PASSWORD", "MCP_AUTH_TOKENS")

MIN_TOKEN_LENGTH = 16
MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Paperless connection ---------------------------------------------------
    paperless_url: str = Field(description="Base URL of Paperless-NGX, e.g. http://paperless:8000")
    paperless_token: SecretStr | None = Field(
        default=None, description="Paperless API token (Profile -> API Auth Token)"
    )
    paperless_username: str | None = None
    paperless_password: SecretStr | None = None
    paperless_public_url: str | None = Field(
        default=None,
        description="Paperless URL as users see it (for share links); default PAPERLESS_URL",
    )
    paperless_verify_ssl: bool = True
    paperless_timeout: float = 30.0
    paperless_upload_timeout: float = Field(
        default=600.0, description="Timeout for streaming large uploads/downloads to Paperless"
    )
    paperless_api_version: int = Field(default=10, ge=9)

    # --- MCP server ---------------------------------------------------------------
    mcp_transport: Literal["http", "stdio"] = "http"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    mcp_public_url: str | None = Field(
        default=None,
        description="Base URL clients use to reach this server (for upload/download links)",
    )
    mcp_auth_tokens: SecretStr | None = Field(
        default=None, description="Comma-separated bearer tokens accepted from MCP clients"
    )
    mcp_allowed_hosts: str | None = Field(
        default=None,
        description="Optional comma-separated Host header allowlist (DNS-rebinding protection)",
    )
    mcp_read_only: bool = False
    mcp_tool_search: Literal["off", "bm25", "regex"] = Field(
        default="off",
        description="Collapse the tool list into search_tools + call_tool for clients "
        "without native tool search",
    )
    mcp_tool_search_max_results: int = Field(default=5, ge=1, le=50)

    # --- Files ----------------------------------------------------------------------
    mcp_max_upload_mb: float = Field(default=100.0, gt=0)
    mcp_max_inline_mb: float = Field(
        default=10.0, gt=0, description="Cap for base64 uploads and inline downloads"
    )
    mcp_upload_dir: Path = Path("/tmp/paperless-mcp")
    mcp_upload_slot_ttl: int = Field(default=900, ge=30, description="Seconds a link is valid")
    mcp_max_concurrent_uploads: int = Field(default=2, ge=1)
    mcp_allow_url_fetch: bool = Field(
        default=True,
        description="Expose upload_document_from_url (public HTTPS only, SSRF-guarded)",
    )
    mcp_url_fetch_timeout: float = Field(default=300.0, gt=0)
    mcp_local_upload_roots: str | None = Field(
        default=None,
        description="stdio only: comma-separated directories upload_document_from_path may read",
    )
    log_level: str = "INFO"

    @field_validator(
        "paperless_token",
        "paperless_username",
        "paperless_password",
        "mcp_auth_tokens",
        "mcp_local_upload_roots",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, v: object) -> object:
        """`PAPERLESS_TOKEN=` (blank, as in .env.example) means 'not set'."""
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("paperless_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v.endswith("/api"):
            v = v[: -len("/api")]
        if not v.startswith(("http://", "https://")):
            raise ValueError("PAPERLESS_URL must start with http:// or https://")
        return v

    @field_validator("mcp_public_url", "paperless_public_url")
    @classmethod
    def _strip_public_url(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("public URLs must start with http:// or https://")
        return v

    @field_validator("mcp_path")
    @classmethod
    def _norm_path(cls, v: str) -> str:
        return "/" + v.strip().strip("/")

    @model_validator(mode="after")
    def _check_combinations(self) -> Settings:
        if self.paperless_token is None and not (
            self.paperless_username and self.paperless_password
        ):
            raise ValueError(
                "Set either PAPERLESS_TOKEN or both PAPERLESS_USERNAME and PAPERLESS_PASSWORD"
            )
        if self.mcp_transport == "http":
            tokens = self.auth_tokens
            if not tokens:
                raise ValueError(
                    "MCP_AUTH_TOKENS is required for the http transport "
                    "(generate one with: openssl rand -hex 32)"
                )
            if any(len(t) < MIN_TOKEN_LENGTH for t in tokens):
                raise ValueError(f"Each MCP auth token must be at least {MIN_TOKEN_LENGTH} chars")
        return self

    @property
    def auth_tokens(self) -> list[str]:
        if self.mcp_auth_tokens is None:
            return []
        raw = self.mcp_auth_tokens.get_secret_value()
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def allowed_hosts(self) -> list[str] | None:
        if not self.mcp_allowed_hosts:
            return None
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def local_upload_roots(self) -> list[Path]:
        if not self.mcp_local_upload_roots:
            return []
        return [
            Path(p.strip()).expanduser().resolve()
            for p in self.mcp_local_upload_roots.split(",")
            if p.strip()
        ]

    @property
    def max_upload_bytes(self) -> int:
        return int(self.mcp_max_upload_mb * MB)

    @property
    def max_inline_bytes(self) -> int:
        return int(self.mcp_max_inline_mb * MB)


def load_settings(**overrides: object) -> Settings:
    """Build Settings, resolving `<SECRET>_FILE` env vars into their secret values."""
    file_values: dict[str, object] = {}
    for name in _FILE_SECRETS:
        file_path = os.environ.get(f"{name}_FILE")
        if file_path and not os.environ.get(name):
            file_values[name.lower()] = Path(file_path).read_text().strip()
    return Settings(**{**file_values, **overrides})  # type: ignore[arg-type]
