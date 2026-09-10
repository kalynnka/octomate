"""What every provider's OAuth shares, below the flows themselves.

A credential is worth little at rest: it has to be carried to an upstream, and the
upstream is the only thing that can say it has stopped working. That round trip
belongs to no single provider, so it lives here rather than in one of them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import httpx2

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
