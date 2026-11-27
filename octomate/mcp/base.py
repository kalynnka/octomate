"""What every Octomate MCP server shares: the bearer verifier for API keys and OAuth
tokens, and the docstring projection that keeps a spell's MCP tool and its
Inkling tool identical."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import cleandoc

from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import SecretStr

from octomate.managers.auth import AuthManager
from octomate.managers.user import UserManager
from octomate.types.auth import ApiKeyScope

# The name every runtime mounts the server under. Claude and dsh name a server's
# tools `mcp__<server>__<tool>`, Codex namespaces them `mcp__<server>`.
OCTOMATE_SERVER_NAME = "octomate"
# The one endpoint: the host mounts the server's app under its name, and the
# transport answers at `/mcp` inside it. Every install config copies this literal.
OCTOMATE_MCP_PATH = f"/{OCTOMATE_SERVER_NAME}/mcp"


class KnownBearers(TokenVerifier):
    """Verify API keys and resource-bound OAuth tokens through the auth manager."""

    def __init__(self, auth: AuthManager | None = None, *, users: UserManager) -> None:
        super().__init__(required_scopes=["mcp"])
        self.auth: AuthManager | None = auth
        self.users: UserManager = users

    async def owner(self, token: str, *, scope: ApiKeyScope) -> str | None:
        if self.auth is None:
            return None
        key = await self.auth.authenticate_api_key(SecretStr(token), scope=scope)
        if key is None:
            return None
        user = await self.users.get(key.user_id)
        return user.username if user is not None else None

    async def verify_token(self, token: str) -> AccessToken | None:
        owner = await self.owner(token, scope="mcp")
        if owner is not None:
            return AccessToken(
                token=token, client_id=owner, subject=owner, scopes=["mcp"]
            )
        if self.auth is None or self.auth.config.oauth_issuer_url is None:
            return None
        issuer = str(self.auth.config.oauth_issuer_url).rstrip("/")
        principal = await self.auth.authenticate_oauth_token(
            SecretStr(token), resource=f"{issuer}{OCTOMATE_MCP_PATH}", scope="mcp"
        )
        if principal is None:
            return None
        user = await self.users.get(principal.user_id)
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(principal.client_id),
            subject=user.username,
            scopes=list(principal.scopes),
            expires_at=int(principal.expires_at.timestamp()),
            resource=principal.resource,
            claims={"iss": str(self.auth.config.oauth_issuer_url)},
        )


def capability_contract(spell: Callable[..., Awaitable[object]]) -> str:
    """The docstring Inkling's toolset compiles, verbatim — a copy here would give
    two models two different tools and drift silently."""
    doc = spell.__doc__
    if doc is None:
        raise RuntimeError(f"{spell.__qualname__} has no docstring to project")
    return cleandoc(doc)
