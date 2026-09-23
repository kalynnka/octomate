"""Discord's token exchange and account identity handling."""

from __future__ import annotations

from typing import Literal

import httpx2
from pydantic import BaseModel, Field

from octomate.managers.oauth import OAuthConnector
from octomate.oauth.flows import OAuthTokenExchange
from octomate.schemas.oauth import OAuthGrant
from octomate.schemas.user import UserProfile


class DiscordIdentity(BaseModel):
    """The user `/users/@me` names for a token; a bot account fails validation."""

    id: str = Field(pattern=r"^[1-9][0-9]*$")
    username: str = Field(min_length=1)
    global_name: str | None = None
    bot: Literal[False] = False


class DiscordOAuthGrant(OAuthGrant):
    """A grant carrying the Discord identity verified for its token."""

    identity: DiscordIdentity = Field(
        description="User verified by Discord's /users/@me with the granted token."
    )


class DiscordOAuthConnector(OAuthConnector):
    """The Discord connector; linking a profile reads the identity already
    verified on the grant."""

    async def resolve_profile(self, grant: OAuthGrant) -> UserProfile:
        if not isinstance(grant, DiscordOAuthGrant):
            raise ValueError(
                "Discord profile linking requires a verified Discord grant"
            )
        return UserProfile(
            channel_user_id=grant.identity.id,
            name=grant.identity.global_name or grant.identity.username,
            nickname=grant.identity.username,
        )


class DiscordTokenExchange(OAuthTokenExchange):
    """Discord's token exchange: requires an `identify` bearer token and names
    the account via `/users/@me`."""

    async def grant(self, response: httpx2.Response) -> DiscordOAuthGrant:
        grant = await super().grant(response)
        if "identify" not in grant.scopes or grant.token_type.lower() != "bearer":
            raise ValueError("Discord authorization requires an identify bearer token")
        async with self.httpx_client_factory() as client:
            identity_response = await self.send(
                client,
                client.build_request(
                    "GET",
                    "https://discord.com/api/v10/users/@me",
                    headers={
                        "Authorization": f"Bearer {grant.access_token.get_secret_value()}"
                    },
                ),
            )
            identity_response.raise_for_status()
        identity = DiscordIdentity.model_validate_json(identity_response.content)
        grant.subject = identity.id
        grant.account_label = identity.username
        return DiscordOAuthGrant(
            **grant.model_dump(),
            discovery_state=grant.discovery_state,
            identity=identity,
        )
