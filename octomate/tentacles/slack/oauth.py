"""Slack's token response and account identity handling."""

from __future__ import annotations

from typing import Literal

import httpx2
from pydantic import BaseModel, TypeAdapter

from octomate.oauth.mcp import OAuthTokenExchange
from octomate.schemas.oauth import OAuthGrant


class SlackAuthedUser(BaseModel):
    id: str
    scope: str = ""


class SlackUserTokenResponse(BaseModel):
    ok: Literal[True]
    authed_user: SlackAuthedUser | None = None


class SlackErrorResponse(BaseModel):
    ok: Literal[False]
    error: str


class SlackAuthTestResponse(BaseModel):
    ok: Literal[True]
    user: str
    user_id: str
    team: str
    team_id: str


SLACK_AUTH_TEST_ADAPTER: TypeAdapter[SlackAuthTestResponse | SlackErrorResponse] = (
    TypeAdapter(SlackAuthTestResponse | SlackErrorResponse)
)


class SlackTokenExchange(OAuthTokenExchange):
    async def grant(self, response: httpx2.Response) -> OAuthGrant:
        grant = await super().grant(response)
        token = SlackUserTokenResponse.model_validate_json(response.content)
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
        grant.subject = identity.user_id
        grant.account_label = f"{identity.user} in {identity.team}"
        return grant
