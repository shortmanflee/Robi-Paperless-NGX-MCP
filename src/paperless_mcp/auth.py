"""Inbound authentication: static bearer tokens for LAN clients."""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier


class StaticBearerVerifier(TokenVerifier):
    """Accept requests whose `Authorization: Bearer <token>` matches a configured secret.

    Several tokens may be configured so they can be rotated (or issued per client)
    without downtime. Comparison is constant-time.
    """

    def __init__(self, tokens: list[str]):
        super().__init__()
        if not tokens:
            raise ValueError("StaticBearerVerifier requires at least one token")
        self._tokens = [t.encode() for t in tokens]

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        candidate = token.strip().encode()
        matched: int | None = None
        # Compare against every token (no early exit) to keep timing uniform.
        for idx, expected in enumerate(self._tokens):
            if hmac.compare_digest(candidate, expected) and matched is None:
                matched = idx
        if matched is None:
            return None
        return AccessToken(token=token, client_id=f"lan-client-{matched}", scopes=[])
