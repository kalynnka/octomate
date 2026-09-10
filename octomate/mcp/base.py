from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import cleandoc

from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import SecretStr

from octomate.database import async_session
from octomate.managers.auth import AuthManager
from octomate.schemas.user import User
from octomate.types.auth import ApiKeyScope


class KnownBearers(TokenVerifier):
    """Verify scoped API tokens through the application's auth manager."""

    def __init__(self, auth: AuthManager | None = None) -> None:
        super().__init__()
        self.auth: AuthManager | None = auth

    async def owner(self, token: str, *, scope: ApiKeyScope) -> str | None:
        if self.auth is None:
            return None
        key = await self.auth.authenticate_api_key(SecretStr(token), scope=scope)
        if key is None:
            return None
        async with async_session() as session:
            user = await session.get(User, key.user_id)
        return user.username if user is not None else None

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = await self.owner(token, scope="mcp")
        if principal is None:
            return None
        return AccessToken(token=token, client_id=principal, scopes=["mcp"])


def capability_contract(spell: Callable[..., Awaitable[object]]) -> str:
    """The docstring Inkling's toolset compiles, verbatim — a copy here would give
    two models two different tools and drift silently."""
    doc = spell.__doc__
    if doc is None:
        raise RuntimeError(f"{spell.__qualname__} has no docstring to project")
    return cleandoc(doc)
