"""GitHub's OAuth device flow: the upstream calls a connector composes.

The provider half of the GitHub integration — device-code request, token exchange,
and the account lookup that names the connection. It knows nothing about agents,
capabilities or MCP: application bootstrap composes this flow into an
`OAuthConnector`, registers it on the `OAuthManager`, and hands that connector to
`GitHubCapability`, which is what turns a connection into tools.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from pydantic import AnyHttpUrl, BaseModel, SecretStr, TypeAdapter

from octomate.schemas.oauth import (
    DeviceAuthorizationResponse,
    DeviceOAuthFlow,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
)

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_API_VERSION = "2022-11-28"


class GitHubDeviceCodeResponse(BaseModel):
    device_code: SecretStr
    user_code: SecretStr
    verification_uri: AnyHttpUrl
    expires_in: int
    interval: int = 5


class GitHubAccessTokenResponse(BaseModel):
    access_token: SecretStr
    token_type: str = "bearer"
    scope: str = ""


class GitHubDeviceErrorResponse(BaseModel):
    error: str
    error_description: str = ""
    interval: int | None = None


class GitHubUserResponse(BaseModel):
    id: int
    login: str


GITHUB_TOKEN_RESPONSE = TypeAdapter(
    GitHubAccessTokenResponse | GitHubDeviceErrorResponse
)


class GitHubDeviceOAuthFlow(DeviceOAuthFlow):
    """GitHub OAuth App device authorization and token exchange."""

    def __init__(
        self,
        *,
        client_id: str,
        scopes: list[str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.scopes = scopes
        self.transport = transport

    async def start(self, context: OAuthFlowContext) -> DeviceAuthorizationResponse:
        data = {"client_id": self.client_id}
        if self.scopes:
            data["scope"] = " ".join(self.scopes)
        async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
            response = await client.post(
                GITHUB_DEVICE_CODE_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        authorization = GitHubDeviceCodeResponse.model_validate_json(response.content)
        return DeviceAuthorizationResponse(
            verification_uri=authorization.verification_uri,
            device_code=authorization.device_code,
            user_code=authorization.user_code,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=authorization.expires_in),
            interval_seconds=authorization.interval,
        )

    async def complete(
        self,
        context: OAuthFlowContext,
        device_code: SecretStr,
    ) -> OAuthGrant | OAuthPending:
        async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
            response = await client.post(
                GITHUB_ACCESS_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "device_code": device_code.get_secret_value(),
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            token_response = GITHUB_TOKEN_RESPONSE.validate_json(response.content)
            if isinstance(token_response, GitHubDeviceErrorResponse):
                if token_response.error == "authorization_pending":
                    return OAuthPending(
                        retry_after_seconds=token_response.interval or 5
                    )
                if token_response.error == "slow_down":
                    return OAuthPending(
                        retry_after_seconds=(token_response.interval or 5) + 5
                    )
                detail = token_response.error_description or token_response.error
                raise ValueError(f"GitHub authorization failed: {detail}")

            user_response = await client.get(
                GITHUB_USER_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": (
                        f"Bearer {token_response.access_token.get_secret_value()}"
                    ),
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            )
            user_response.raise_for_status()
        account = GitHubUserResponse.model_validate_json(user_response.content)
        return OAuthGrant(
            access_token=token_response.access_token,
            token_type=token_response.token_type,
            scopes=[
                scope
                for value in token_response.scope.split(",")
                if (scope := value.strip())
            ],
            subject=str(account.id),
            account_label=account.login,
        )
