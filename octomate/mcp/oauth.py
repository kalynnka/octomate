"""Linking a person's accounts as MCP: the one place a runtime asks for an
authorization link, whichever provider's tools it was after.

Every provider the server proxies takes a user token and nothing else, so its
tools are listed only to a person who has linked their account and act as that
person; this family is how the link happens. A provider is a tentacle, and
`connect` names it by its id — the connector its tokens live under — takes the
person the turn is for from the session, and sends the authorization link as a
card to the person's direct messages on the channel the turn is on, never
through the return value the model reads. `confirm` reports whether it went
through.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.managers.gateway import GatewaySession
from octomate.schemas.oauth import AuthorizationLink
from octomate.schemas.user import UserProfile

if TYPE_CHECKING:
    # The tentacle component imports this module for the served tool names it
    # tells a refused caller; only the type is needed back.
    from octomate.tentacles.mcp import McpTentacle

# The family's namespace on the server, and its two tools' served names under it.
OAUTH_NAMESPACE = "oauth"
CONNECT_TOOL = f"{OAUTH_NAMESPACE}_connect"
CONFIRM_TOOL = f"{OAUTH_NAMESPACE}_confirm"

# Which provider a call is about. The deployment's own ids are in the tool
# descriptions, which are composed at mount time; the schema stays constant.
ProviderId = Annotated[
    str, Field(description="The provider's id, as the instructions list them.")
]

# The linking contract every provider shares, templated only where a tool or a
# provider is named.
OAUTH_INSTRUCTION_TEMPLATE = """\
## Linking accounts

A provider's tools — {labels} — are listed here only to a person who has linked
their account with it once, and act as that person. When they are not listed, or
a call is refused for that, call `{connect}` with the provider's id ({ids}),
then tell the person the link is in their direct messages — you are not given it
and cannot repeat or rebuild it. Opening the link and approving is the whole of
it; `{confirm}` says whether it went through, and the tools are listed from
their next turn on.
"""


def oauth_instructions(tentacles: Sequence[McpTentacle]) -> str:
    """The linking instruction for `tentacles`, the providers the server proxies."""
    return OAUTH_INSTRUCTION_TEMPLATE.format(
        labels=", ".join(dict.fromkeys(tentacle.label for tentacle in tentacles)),
        ids=", ".join(f"`{tentacle.id}`" for tentacle in tentacles),
        connect=CONNECT_TOOL,
        confirm=CONFIRM_TOOL,
    )


def mount_oauth(
    mcp: FastMCP,
    gateway_session: GatewaySession,
    tentacles: Sequence[McpTentacle],
) -> None:
    """Register the link tools on `mcp` for `tentacles`, the providers the server
    proxies, every call resolved through `gateway_session` to the turn it belongs
    to."""
    providers = {tentacle.id: tentacle for tentacle in tentacles}
    ids = ", ".join(f"`{id}`" for id in providers)

    def named(provider: str) -> McpTentacle:
        tentacle = providers.get(provider)
        if tentacle is None:
            raise ToolError(
                f"No provider with id {provider!r} is served here; the providers "
                f"are {ids}."
            )
        return tentacle

    def person(session: GatewaySession) -> UserProfile:
        if session.user_profile is None:
            raise ToolError(
                "A link authorizes the person who drove this turn, and nobody "
                "registered did."
            )
        return session.user_profile

    @mcp.tool(
        name="connect",
        description=(
            f"Send this user a link that authorizes their own account with a "
            f"provider — one of {ids}. The link goes to their direct messages, "
            "never to the conversation, and is not returned here."
        ),
    )
    async def connect(
        provider: ProviderId, session: GatewaySession = gateway_session
    ) -> str:
        tentacle = named(provider)
        profile = person(session)
        address = session.conversation_address
        channel = (
            session.channels.get(address.channel_tentacle_id)
            if address is not None
            else None
        )
        if address is None or channel is None:
            raise ToolError(
                "The link goes to the person's direct messages on the channel "
                "this turn is on, and this call has no turn on a channel."
            )
        authorization = await tentacle.octomate.oauth.start(profile, tentacle.id)
        if not isinstance(authorization, AuthorizationLink):
            raise TypeError(f"{tentacle.id} is not on an authorization-code connector")
        # The link goes to the channel as an authorization of its own, for the
        # channel to present — never through this return value, which the model
        # reads and could repeat into a reply.
        await channel.feelers.oauth.present(
            address,
            OAuthAuthorizationEvent(
                connector_id=tentacle.id,
                label=tentacle.label,
                authorization_uri=str(authorization.authorization_uri),
            ),
        )
        return "The authorization link is on its way to this user's direct messages."

    @mcp.tool(
        name="confirm",
        description=(
            f"Report whether this user's connection with a provider — one of "
            f"{ids} — has finished."
        ),
    )
    async def confirm(
        provider: ProviderId, session: GatewaySession = gateway_session
    ) -> str:
        tentacle = named(provider)
        status = await tentacle.octomate.oauth.connection_status(
            person(session), tentacle.id
        )
        if status == "active":
            return (
                f"{tentacle.label} is connected: its tools now act as this user here."
            )
        if status == "invalid":
            return (
                f"{tentacle.label} was connected and is not any more — the "
                "authorization was revoked or expired. Offer to send a fresh "
                f"link with `{CONNECT_TOOL}`."
            )
        return (
            f"{tentacle.label} is not connected yet. The link finishes the "
            "connection by itself once they approve it; there is nothing to do "
            "here but wait and check again."
        )
