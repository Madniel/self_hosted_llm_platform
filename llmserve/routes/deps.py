"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Header, Request

from ..errors import AuthenticationError
from ..service import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Bearer-token auth. No configured keys means auth is disabled."""
    keys = request.app.state.ctx.settings.api_keys
    if not keys:
        return
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token or token not in keys:
        raise AuthenticationError("invalid or missing API key")
