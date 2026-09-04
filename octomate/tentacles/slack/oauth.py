"""Slack's authorization-code OAuth for user tokens: the upstream a workspace's
connector composes.

The provider half of a Slack channel's own MCP. Slack's MCP server takes a user
token and nothing else — every call acts as the person who authorized it, never
as the bot — so the flow is per user like Linear's, with the app's client secret
making Octomate the confidential client Slack requires. The endpoints, the S256
challenge and the `client_secret_post` exchange are what Slack advertises at
`https://mcp.slack.com/.well-known/oauth-authorization-server`.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal

import httpx
from pydantic import AnyHttpUrl, BaseModel, SecretStr, TypeAdapter

from octomate.config.channels import SlackUserScope
from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationRequest,
    OAuthFlowContext,
    OAuthGrant,
)

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2_user/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.user.access"
SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"

# How long a user has to finish an authorization before the link stops working.
# Ours to choose, as with Linear: the deadline is on the operation holding the
# PKCE verifier, not on anything upstream.
AUTHORIZATION_LIFETIME = timedelta(minutes=10)


class SlackAuthedUser(BaseModel):
    id: str
    # Comma-separated, Slack's way; `granted_scopes` on the response splits it.
    scope: str = ""


class SlackUserTokenResponse(BaseModel):
    """`oauth.v2.user.access` when it succeeds: the token at the top level, unlike
    the bot-token method that nests a user's under `authed_user`. A refresh token
    and an expiry arrive only when the app has token rotation switched on."""

    ok: Literal[True]
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: str = "user"
    expires_in: int | None = None
    authed_user: SlackAuthedUser | None = None

    @property
    def granted_scopes(self) -> list[str]:
        if self.authed_user is None:
            return []
        return [
            scope
            for value in self.authed_user.scope.split(",")
            if (scope := value.strip())
        ]


class SlackErrorResponse(BaseModel):
    """Every Slack Web API method's failure: a 200 whose body says `ok: false`."""

    ok: Literal[False]
    error: str


class SlackAuthTestResponse(BaseModel):
    ok: Literal[True]
    user: str
    user_id: str
    team: str
    team_id: str


# Annotated for the reason `LINEAR_TOKEN_RESPONSE_ADAPTER` is: an adapter over a
# bare union validates to `Unknown`, and the branches below stop being checked.
SLACK_TOKEN_RESPONSE_ADAPTER: TypeAdapter[
    SlackUserTokenResponse | SlackErrorResponse
] = TypeAdapter(SlackUserTokenResponse | SlackErrorResponse)
SLACK_AUTH_TEST_ADAPTER: TypeAdapter[SlackAuthTestResponse | SlackErrorResponse] = (
    TypeAdapter(SlackAuthTestResponse | SlackErrorResponse)
)


class SlackAuthorizationCodeOAuthFlow(AuthorizationCodeOAuthFlow):
    """Slack user-token authorization, token exchange and refresh for one app."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        scopes: list[SlackUserScope],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.transport = transport

    async def start(
        self,
        context: OAuthFlowContext,
        callback_uri: AnyHttpUrl,
        state: SecretStr,
    ) -> AuthorizationRequest:
        # PKCE on top of the client secret: Slack advertises S256, and the callback
        # is a plain browser GET, so the code alone should be worth nothing to
        # whoever else it reaches.
        verifier = token_urlsafe(64)
        challenge = (
            urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
        )
        authorization_uri = httpx.URL(
            SLACK_AUTHORIZE_URL,
            params={
                "client_id": self.client_id,
                "redirect_uri": str(callback_uri),
                "response_type": "code",
                # Space-delimited as RFC 6749 has it: this endpoint is the one
                # Slack points standard MCP clients at, which spell it no other way.
                "scope": " ".join(self.scopes),
                "state": state.get_secret_value(),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        return AuthorizationRequest(
            authorization_uri=AnyHttpUrl(str(authorization_uri)),
            code_verifier=SecretStr(verifier),
            expires_at=datetime.now(UTC) + AUTHORIZATION_LIFETIME,
        )

    async def exchange(
        self,
        context: OAuthFlowContext,
        *,
        code: str,
        code_verifier: SecretStr | None,
        callback_uri: AnyHttpUrl,
    ) -> OAuthGrant:
        data = {
            "code": code,
            "redirect_uri": str(callback_uri),
            "client_id": self.client_id,
            "client_secret": self.client_secret.get_secret_value(),
            "grant_type": "authorization_code",
        }
        if code_verifier is not None:
            data["code_verifier"] = code_verifier.get_secret_value()
        return await self.grant(data)

    async def refresh(self, refresh_token: SecretStr) -> OAuthGrant:
        grant = await self.grant(
            {
                "refresh_token": refresh_token.get_secret_value(),
                "client_id": self.client_id,
                "client_secret": self.client_secret.get_secret_value(),
                "grant_type": "refresh_token",
            }
        )
        if grant.refresh_token is not None:
            return grant
        # RFC 6749 lets an authorization server omit the refresh token when it has
        # not rotated one, which means keep the one just spent. Dropping it instead
        # would leave a connection that can never refresh again.
        return grant.model_copy(update={"refresh_token": refresh_token})

    async def grant(self, data: dict[str, str]) -> OAuthGrant:
        """Post to the token endpoint and name whoever the resulting token belongs to.

        Shared by the two ways of getting one. `auth.test` names the account the
        way Slack itself does — user id and handle, workspace too — which the token
        response leaves out on some paths, and proves the token works before it is
        stored. A refresh writes the whole connection back, so it rides along there
        as well.
        """
        async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
            response = await client.post(
                SLACK_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
            if response.status_code >= 500:
                response.raise_for_status()
            token_response = SLACK_TOKEN_RESPONSE_ADAPTER.validate_json(
                response.content
            )
            if isinstance(token_response, SlackErrorResponse):
                raise ValueError(f"Slack authorization failed: {token_response.error}")

            identity_response = await client.post(
                SLACK_AUTH_TEST_URL,
                headers={
                    "Authorization": (
                        f"Bearer {token_response.access_token.get_secret_value()}"
                    ),
                },
            )
            identity_response.raise_for_status()
        identity = SLACK_AUTH_TEST_ADAPTER.validate_json(identity_response.content)
        if isinstance(identity, SlackErrorResponse):
            raise ValueError(
                f"Slack would not name the account its token is for: {identity.error}"
            )
        return OAuthGrant(
            access_token=token_response.access_token,
            refresh_token=token_response.refresh_token,
            token_type=token_response.token_type,
            scopes=token_response.granted_scopes,
            subject=identity.user_id,
            account_label=f"{identity.user} in {identity.team}",
            expires_at=(
                datetime.now(UTC) + timedelta(seconds=token_response.expires_in)
                if token_response.expires_in is not None
                else None
            ),
        )
