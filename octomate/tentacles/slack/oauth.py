"""Slack's token response and account identity handling."""

from __future__ import annotations

from typing import ClassVar, Literal

import httpx2
from mcp.shared.auth import OAuthToken
from pydantic import BaseModel, Field, TypeAdapter, field_validator

from octomate.managers.oauth import OAuthConnector
from octomate.oauth.flows import OAuthTokenExchange, TokenError
from octomate.schemas.oauth import OAuthGrant
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.slack.schema import SlackUserProfile


class SlackAuthedUser(BaseModel):
    id: str
    scope: str = ""


class SlackUserToken(OAuthToken):
    ok: Literal[True]
    authed_user: SlackAuthedUser | None = None

    @field_validator("token_type", mode="before")
    @classmethod
    def normalize_user_token_type(cls, value: str | None) -> str | None:
        # Slack labels its user bearer tokens by account type.
        return "Bearer" if value == "user" else value


class SlackErrorResponse(BaseModel):
    ok: Literal[False]
    error: str


class SlackAuthTestResponse(BaseModel):
    ok: Literal[True]
    user: str
    user_id: str = Field(min_length=1)
    team: str
    team_id: str = Field(min_length=1)
    bot_id: str | None = None


SLACK_AUTH_TEST_ADAPTER: TypeAdapter[SlackAuthTestResponse | SlackErrorResponse] = (
    TypeAdapter(SlackAuthTestResponse | SlackErrorResponse)
)


class SlackOAuthGrant(OAuthGrant):
    team_id: str = Field(
        min_length=1,
        description="Workspace verified by auth.test with the granted token.",
    )


class SlackOAuthConnector(OAuthConnector):
    ink: SlackInk

    async def resolve_profile(self, grant: OAuthGrant) -> SlackUserProfile:
        if not isinstance(grant, SlackOAuthGrant) or not grant.subject:
            raise ValueError("Slack profile linking requires a verified Slack grant")
        response = await self.ink.client.auth_test()
        bot = SLACK_AUTH_TEST_ADAPTER.validate_python(response.data)
        if isinstance(bot, SlackErrorResponse):
            raise ValueError("Slack could not verify this channel's workspace")
        if bot.team_id != grant.team_id or bot.user_id == grant.subject:
            raise ValueError(
                "The Slack account does not belong to this channel's workspace"
            )
        return await self.ink.get_user_profile(grant.subject)


class SlackTokenExchange(OAuthTokenExchange):
    token_model: ClassVar[type[OAuthToken]] = SlackUserToken

    async def grant(
        self, response: httpx2.Response, token: OAuthToken | TokenError | None = None
    ) -> SlackOAuthGrant:
        if token is None:
            token = self.parse_response(response)
        grant = await super().grant(response, token)
        if not isinstance(token, SlackUserToken):
            raise ValueError("Slack authorization requires a Slack user token")
        grant.scopes = (
            [
                scope.strip()
                for scope in token.authed_user.scope.split(",")
                if scope.strip()
            ]
            if token.authed_user is not None
            else []
        )
        async with self.httpx_client_factory() as client:
            identity_response = await self.send(
                client,
                client.build_request(
                    "POST",
                    "https://slack.com/api/auth.test",
                    headers={
                        "Authorization": f"Bearer {grant.access_token.get_secret_value()}"
                    },
                ),
            )
            identity_response.raise_for_status()
        identity = SLACK_AUTH_TEST_ADAPTER.validate_json(identity_response.content)
        if isinstance(identity, SlackErrorResponse):
            raise ValueError(
                f"Slack would not name the account its token is for: {identity.error}"
            )
        if identity.bot_id is not None:
            raise ValueError("Slack profile authorization requires a user token")
        grant.subject = identity.user_id
        grant.account_label = f"{identity.user} in {identity.team}"
        return SlackOAuthGrant(
            **grant.model_dump(),
            discovery_state=grant.discovery_state,
            team_id=identity.team_id,
        )
