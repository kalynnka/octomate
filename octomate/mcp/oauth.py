"""Authorize the current user's installed MCP by its personal namespace.

Authorization links and device codes go to the user's direct messages.
The model receives status only; device flows finish through `confirm`.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from octomate.capabilities.harness.events import (
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
)
from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.schemas.mcp import (
    McpAuthorizationStatus,
    McpBrowserAuthorizationPending,
    McpDeviceAuthorizationPending,
    OAuthMcp,
)
from octomate.schemas.oauth import (
    AuthorizationLink,
)
from octomate.schemas.user import User, UserProfile

# The family's namespace on the server, and its two tools' served names under it.
OAUTH_NAMESPACE = "oauth"
CONNECT_TOOL = f"{OAUTH_NAMESPACE}_connect"
CONFIRM_TOOL = f"{OAUTH_NAMESPACE}_confirm"

ProviderId = Annotated[str, Field(description="The user's installed MCP namespace.")]


def mount_oauth(
    mcp: FastMCP,
    octomate_session: OctomateSession,
    manager: McpManager,
) -> None:
    """Authorize installed MCPs for the person identified by the current session."""

    async def identity(session: OctomateSession) -> tuple[User, UserProfile]:
        if session.user_profile is None:
            raise ToolError(
                "A link authorizes the person who drove this turn, and nobody "
                "registered did."
            )
        user = await manager.users.owner(session.user_profile)
        if user is None:
            raise ToolError("OAuth connections require a registered user")
        return user, session.user_profile

    async def personal(provider: str, user: User) -> OAuthMcp:
        try:
            return await manager.authorizable(user_id=user.id, namespace=provider)
        except McpUnavailable as error:
            raise ToolError("This OAuth MCP is unavailable") from error

    @mcp.tool(
        name="connect",
        description=(
            "Send this user a link that authorizes their own account with a "
            "provider identified by its installed MCP namespace. The link, and a code where the provider asks "
            "for one, go to their direct messages, never to the conversation, and "
            "are not returned here."
        ),
    )
    async def connect(
        provider: ProviderId, session: OctomateSession = octomate_session
    ) -> str:
        user, profile = await identity(session)
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
        instance = await personal(provider, user)
        try:
            authorization = await manager.connect(user, instance.id, profile=profile)
        except McpUnavailable as error:
            raise ToolError(str(error)) from error
        label = instance.name
        # The authorization goes to the channel as an event of its own, for the
        # channel to present — never through this return value, which the model
        # reads and could repeat into a reply.
        if isinstance(authorization, AuthorizationLink):
            event = OAuthAuthorizationEvent(
                connector_id=provider,
                label=label,
                authorization_uri=str(authorization.authorization_uri),
            )
            sent = "The authorization link is on its way"
        else:
            event = OAuthDeviceAuthorizationEvent(
                connector_id=provider,
                label=label,
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
        description="Check authorization for a user's installed MCP namespace, polling when required.",
    )
    async def confirm(
        provider: ProviderId, session: OctomateSession = octomate_session
    ) -> str:
        user, profile = await identity(session)
        instance = await personal(provider, user)
        status = await manager.confirm(user, instance.id, profile=profile)
        match status:
            case McpAuthorizationStatus(status="active"):
                return f"{provider} is connected and available through namespace discovery."
            case McpDeviceAuthorizationPending(retry_after_seconds=delay):
                return f"{provider} is waiting for approval; confirm again in {delay} seconds."
            case McpBrowserAuthorizationPending():
                return f"{provider} is waiting for approval in the browser. Confirm after approval."
            case _:
                return f"{provider} needs authorization. Call `{CONNECT_TOOL}` with `{provider}`."
