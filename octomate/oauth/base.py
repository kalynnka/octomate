"""What every provider's OAuth shares, below the flows themselves.

A credential is worth little at rest: it has to be carried to an upstream, and the
upstream is the only thing that can say it has stopped working. That round trip
belongs to no single provider, so it lives here rather than in one of them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING

import httpx2
from pydantic import SecretStr

from octomate.schemas.user import User

if TYPE_CHECKING:
    from octomate.managers.oauth import OAuthManager


class McpBearerAuth(httpx2.Auth):
    def __init__(
        self,
        *,
        manager: OAuthManager,
        user: User,
        mcp_id: uuid.UUID,
        connector_id: str,
        url: str,
    ) -> None:
        self.manager = manager
        self.user = user
        self.mcp_id = mcp_id
        self.connector_id = connector_id
        self.url = httpx2.URL(url)

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        if request.url.copy_with(query=None) != self.url:
            raise ValueError("OAuth credentials are bound to the installed MCP URL")
        token = await self.manager.access_token(
            self.user, self.connector_id, mcp_id=self.mcp_id
        )
        if token is None:
            raise ValueError(
                "This MCP needs authorization; connect it before calling its tools"
            )
        request.headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        response = yield request
        if response.status_code == 401:
            await self.manager.invalidate(
                self.user, self.connector_id, mcp_id=self.mcp_id, expected_token=token
            )


class McpConnectionAuth(httpx2.Auth):
    """One user's bearer credentials for an MCP session, and the 401 that ends them.

    Sitting on the transport rather than in a tool hook is what makes this cover
    the whole session: the same 401 answers the `initialize` that warms a session
    and the tool call that uses it, and only one of those two is anywhere a tool
    hook can see. A proxied listing that fails is an empty listing, so a revoked
    token would otherwise fail quietly on every turn forever.

    A 401 answering a bearer token is the provider saying the credential is gone,
    and there is nothing to retry — so it is reported once and the request is left
    to fail on its own terms.
    """

    def __init__(
        self,
        access_token: SecretStr,
        on_unauthorized: Callable[[], Awaitable[None]],
    ) -> None:
        self.access_token = access_token
        self.on_unauthorized = on_unauthorized
        self.reported = False

    async def async_auth_flow(
        self,
        request: httpx2.Request,
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = (
            f"Bearer {self.access_token.get_secret_value()}"
        )
        response = yield request
        if response.status_code == 401 and not self.reported:
            self.reported = True
            await self.on_unauthorized()
