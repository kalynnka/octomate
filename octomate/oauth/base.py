"""What every provider's OAuth shares, below the flows themselves.

A credential is worth little at rest: it has to be carried to an upstream, and the
upstream is the only thing that can say it has stopped working. That round trip
belongs to no single provider, so it lives here rather than in one of them.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

import httpx
from pydantic import SecretStr


class McpConnectionAuth(httpx.Auth):
    """One user's bearer credentials for an MCP session, and the 401 that ends them.

    Sitting on the transport rather than in a tool hook is what makes this cover
    the whole session: the same 401 answers the `initialize` that warms a session
    and the tool call that uses it, and only one of those two is anywhere a tool
    hook can see. `McpToolsetCache.warm` logs a failure and moves on, so a revoked
    token would otherwise fail quietly on every run forever.

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
        request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = (
            f"Bearer {self.access_token.get_secret_value()}"
        )
        response = yield request
        if response.status_code == 401 and not self.reported:
            self.reported = True
            await self.on_unauthorized()
