"""Feishu MCP authentication and user identity verification."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

import httpx2
from pydantic import UUID7, BaseModel, Field, SecretStr

from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.oauth.base import McpBearerAuth
from octomate.oauth.flows import OAuthTokenExchange
from octomate.schemas.oauth import OAuthGrant
from octomate.schemas.user import User, UserProfile

if TYPE_CHECKING:
    from octomate.tentacles.lark.base import LarkTentacle


class LarkMcpBearerAuth(McpBearerAuth):
    """The special case Feishu makes ordinary bearer authentication need.

    Feishu's developer-app MCP endpoint carries the user token in
    `X-Lark-MCP-UAT` instead of `Authorization: Bearer ...`. Renaming the header
    adds no protection against a caller holding the token. It does force an
    otherwise generic OAuth client to grow a provider-specific adapter.

    `X-Lark-MCP-Allowed-Tools` carries the comma-separated `mcp_tools` list.
    [Feishu's contract](https://open.feishu.cn/document/mcp_open_tools/developers-call-remote-mcp-server)
    uses it to filter discovery and calls; omitting it exposes no tools.
    That makes it useful for selecting tools. Calling the header "Allowed"
    does not turn a caller's wish list into authorization: a client or agent
    holding the token and controlling HTTP requests can submit a wider list
    with the same credential, including on a new connection. This design adds
    integration work without establishing a separate permission boundary.

    Octomate sets these headers from deployment configuration; proxied tool
    arguments cannot override them. The limitation concerns clients that hold
    the credential themselves. Keep grants narrow using OAuth scopes and the
    provider's app and resource permissions. Changing this header grants none
    of those permissions and does not restrict the token outside this client.
    """

    tools: list[str]

    def __init__(
        self,
        *,
        manager: OAuthManager,
        user: User,
        mcp_id: UUID7,
        url: str,
        tentacle: LarkTentacle,
    ) -> None:
        super().__init__(
            manager=manager,
            user=user,
            mcp_id=mcp_id,
            connector_id=tentacle.id,
            url=url,
        )
        self.tools = tentacle.config.mcp_tools

    def headers(self, token: SecretStr) -> dict[str, str]:
        # A caller-written allowlist is no authority over the token holder.
        # Keep this proprietary tool filter confined to the Feishu adapter.
        return {
            "X-Lark-MCP-UAT": token.get_secret_value(),
            "X-Lark-MCP-Allowed-Tools": ",".join(self.tools),
        }


class LarkOAuthIdentity(BaseModel):
    """The app-scoped user identified by Feishu's user_info endpoint."""

    open_id: str = Field(min_length=1)
    name: str


class LarkUserInfoResponse(BaseModel):
    """A successful user_info response; provider errors fail validation."""

    code: Literal[0]
    data: LarkOAuthIdentity


class LarkOAuthConnector(OAuthConnector):
    """Link the user proven by the app's token, using its app-scoped open ID.

    Feishu's API versions are a compatibility trap. Its
    [token migration advice](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)
    recommends v3, while the `code_challenge` parameter in
    [Get authorization code](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code)
    explicitly recommends pairing PKCE with the **v2 token endpoint** and says
    support for the latest token endpoint will follow. Those recommendations
    contradict each other; choosing the highest version breaks this flow.

    Octomate uses v1 authorization with S256 PKCE, v2 token exchange and refresh,
    and v1 user-info lookup. These version numbers do not describe one shared
    protocol version. In this integration, v3 rejected the authorization code
    despite the saved verifier matching the authorization challenge; switching
    to v2 completed a fresh authorization successfully. Keep PKCE enabled and
    retain the documented v1/v2 pairing until Feishu supports and verifies its
    replacement.

    App credentials are sent in the token request's form body. The MCP headers
    `X-Lark-MCP-UAT` and `X-Lark-MCP-Allowed-Tools` are independent of these OAuth
    endpoint versions.
    """

    httpx_client_factory: Callable[[], httpx2.AsyncClient]

    async def resolve_profile(self, grant: OAuthGrant) -> UserProfile:
        async with self.httpx_client_factory() as client:
            response = await OAuthTokenExchange.send(
                client,
                client.build_request(
                    "GET",
                    "https://open.feishu.cn/open-apis/authen/v1/user_info",
                    headers={
                        "Authorization": f"Bearer {grant.access_token.get_secret_value()}"
                    },
                ),
            )
            response.raise_for_status()
        identity = LarkUserInfoResponse.model_validate_json(response.content).data
        return UserProfile(channel_user_id=identity.open_id, name=identity.name)
