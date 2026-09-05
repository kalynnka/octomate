"""Linking a person's accounts as MCP: the one place a runtime asks for an
authorization link, whichever provider's tools it was after.

A provider that acts as the person takes their own token and nothing else, so
its tools are listed only to a person who has linked their account; this family
is how the link happens. A provider is a tentacle, and `connect` names it by its
id — the connector its tokens live under — takes the person the turn is for from
the session, and sends the authorization as a card to the person's direct
messages on the channel the turn is on, never through the return value the model
reads: a link, or for a device flow a link and a code. `confirm` reports whether
it went through — and for a device flow, finishes it, since the provider only
tells once asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from octomate.capabilities.harness.events import (
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.managers.gateway import GatewaySession
from octomate.managers.oauth import NoPendingAuthorization
from octomate.schemas.oauth import (
    AuthorizationLink,
    DeviceOAuthFlow,
    OAuthPending,
)
from octomate.schemas.user import UserProfile

if TYPE_CHECKING:
    # The tentacle component imports this module for the served tool names it
    # tells a refused caller; only the type is needed back.
    from octomate.tentacles.mcp import OAuthMcpTentacle

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
then tell the person the link — and the code, where a provider asks them to type
one — is in their direct messages; you are not given either and cannot repeat or
rebuild them. Opening the link and approving is the whole of it. `{confirm}` says
whether it went through — call it once the person says they have approved, since
a provider that gave them a code reports only when asked — and the tools are
listed from their next turn on.
"""


def oauth_instructions(tentacles: Sequence[OAuthMcpTentacle]) -> str:
    """The linking instruction for `tentacles`, the ones the server links."""
    return OAUTH_INSTRUCTION_TEMPLATE.format(
        labels=", ".join(dict.fromkeys(tentacle.label for tentacle in tentacles)),
        ids=", ".join(f"`{tentacle.id}`" for tentacle in tentacles),
        connect=CONNECT_TOOL,
        confirm=CONFIRM_TOOL,
    )


def mount_oauth(
    mcp: FastMCP,
    gateway_session: GatewaySession,
    tentacles: Sequence[OAuthMcpTentacle],
) -> None:
    """Register the link tools on `mcp` for `tentacles`, the ones the server
    links, every call resolved through `gateway_session` to the turn it belongs
    to."""
    mapping = {tentacle.id: tentacle for tentacle in tentacles}
    ids = ", ".join(f"`{id}`" for id in mapping)

    def named(provider: str) -> OAuthMcpTentacle:
        tentacle = mapping.get(provider)
        if tentacle is None:
            raise ToolError(
                f"No provider with id {provider!r} is linked here; the providers "
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
            f"provider — one of {ids}. The link, and a code where the provider asks "
            "for one, go to their direct messages, never to the conversation, and "
            "are not returned here."
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
        # The authorization goes to the channel as an event of its own, for the
        # channel to present — never through this return value, which the model
        # reads and could repeat into a reply.
        if isinstance(authorization, AuthorizationLink):
            event = OAuthAuthorizationEvent(
                connector_id=tentacle.id,
                label=tentacle.label,
                authorization_uri=str(authorization.authorization_uri),
            )
            sent = "The authorization link is on its way"
        else:
            event = OAuthDeviceAuthorizationEvent(
                connector_id=tentacle.id,
                label=tentacle.label,
                authorization_uri=str(
                    authorization.verification_uri_complete
                    or authorization.verification_uri
                ),
                user_code=authorization.user_code.get_secret_value(),
            )
            sent = "The authorization link and code are on their way"
        await channel.feelers.oauth.present(address, event)
        return f"{sent} to this user's direct messages."

    @mcp.tool(
        name="confirm",
        description=(
            f"Report whether this user's connection with a provider — one of "
            f"{ids} — has finished, finishing it where the provider waits to be "
            "asked."
        ),
    )
    async def confirm(
        provider: ProviderId, session: GatewaySession = gateway_session
    ) -> str:
        tentacle = named(provider)
        profile = person(session)
        oauth = tentacle.octomate.oauth
        device = isinstance(oauth.connector(tentacle.id).flow, DeviceOAuthFlow)
        if device:
            # A device flow finishes only when asked: the provider is polled for
            # the code the person typed, and a wait is the person's to end.
            try:
                result = await oauth.complete_latest(profile, tentacle.id)
            except NoPendingAuthorization:
                pass  # nothing waiting: the standing connection below is the answer
            else:
                if isinstance(result, OAuthPending):
                    return (
                        f"{tentacle.label} is still waiting for authorization. They "
                        "have to finish it from the link in their direct messages, "
                        f"then try again in {result.retry_after_seconds} seconds."
                    )
                return (
                    f"{tentacle.label} connected as @{result.account_label}: its "
                    "tools now act as this user here."
                )
        status = await oauth.connection_status(profile, tentacle.id)
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
        if device:
            # Confirming before connecting: an ordering the model can fix itself,
            # so say which tool opens one rather than ending the turn.
            raise ToolError(
                f"Nothing to confirm — this user has no {tentacle.label} "
                f"authorization waiting. Call `{CONNECT_TOOL}` with `{tentacle.id}` "
                "to start one, then confirm once they have entered the code."
            )
        return (
            f"{tentacle.label} is not connected yet. The link finishes the "
            "connection by itself once they approve it; there is nothing to do "
            "here but wait and check again."
        )
