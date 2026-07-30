from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import FunctionToolset

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.capabilities.harness.mcp import OAuthMcpCapability
from octomate.config.integrations import LinearMcpConfig
from octomate.config.mcp import McpIntegrationConfig
from octomate.schemas.oauth import AuthorizationCodeOAuthFlow, AuthorizationLink

# How a channel names this integration when it presents its authorization.
LINEAR_LABEL = "Linear"

LINEAR_CONNECT_TOOL = "connect_linear"
LINEAR_CONFIRM_TOOL = "confirm_linear"

# The catalog line the model reads while this capability is deferred — all it knows
# about Linear until it loads it, so it names both the work and the connecting.
LINEAR_CAPABILITY_DESCRIPTION = (
    "Work with this user's own Linear workspace — issues, projects, cycles, comments "
    "— and connect that workspace if they have not linked it yet."
)

# Linear finishes in the browser rather than on a code the user reads out, so the
# model is told to wait rather than to poll: there is nothing for `confirm_linear`
# to advance, only something for it to report.
LINEAR_OAUTH_INSTRUCTION = """\
## Linear connection

Linear tools are unavailable until this user connects their own workspace.
- Call `connect_linear` when the user asks to connect Linear or needs a Linear tool.
- It sends an authorization link directly to this conversation. Do not repeat or
  rebuild the link in your final response.
- Opening that link and approving it is the whole of the connection; nothing here
  finishes it. If the user asks whether it worked, call `confirm_linear` to check.
  Once connected, Linear tools become available on their next message.
"""

LINEAR_RETIRED_INSTRUCTION = """\
## Linear connection

This user WAS connected to Linear and no longer is: Linear rejected their
authorization, so it was revoked, expired, or had access withdrawn. Their Linear
tools are gone until they connect again, and nothing has told them.
- Say so once in your next reply, briefly, even if they asked about something else
  — they cannot see this and may be waiting on work that can no longer happen.
- Offer to reconnect. Call `connect_linear` when they agree; it sends a fresh
  authorization link to this conversation. Do not repeat or rebuild the link in your
  final response.
- Opening that link and approving it is the whole of the connection. Once connected,
  Linear tools become available on their next message.
"""

logger = logging.getLogger(__name__)


@dataclass
class LinearCapability(OAuthMcpCapability):
    """Linear's browser authorization over the shared per-user OAuth/MCP capability."""

    mcp_config: McpIntegrationConfig = field(default_factory=LinearMcpConfig)

    label = LINEAR_LABEL
    connect_tool = LINEAR_CONNECT_TOOL
    confirm_tool = LINEAR_CONFIRM_TOOL
    capability_description = LINEAR_CAPABILITY_DESCRIPTION
    oauth_instruction = LINEAR_OAUTH_INSTRUCTION
    retired_instruction = LINEAR_RETIRED_INSTRUCTION
    flow_type = AuthorizationCodeOAuthFlow

    def build_oauth_toolset(self) -> FunctionToolset[None]:
        toolset: FunctionToolset[None] = FunctionToolset(id="linear-oauth")

        @toolset.tool(name=LINEAR_CONNECT_TOOL)
        async def connect_linear(ctx: RunContext[None]) -> ToolReturn[str]:
            """Send this user a link that authorizes their own Linear workspace."""
            authorization = await self.manager.start(
                self.bound_profile(),
                self.connector.id,
            )
            if not isinstance(authorization, AuthorizationLink):
                raise ValueError(
                    "Linear is not configured for authorization-code OAuth"
                )
            # The link goes to the channel as an authorization of its own, for the
            # channel to present — never through this return value, which the model
            # reads and could repeat into a reply.
            return ToolReturn(
                return_value=(
                    "The authorization link is on its way to this conversation."
                ),
                metadata=[
                    OAuthAuthorizationEvent(
                        connector_id=self.connector.id,
                        label=LINEAR_LABEL,
                        authorization_uri=str(authorization.authorization_uri),
                    )
                ],
            )

        @toolset.tool(name=LINEAR_CONFIRM_TOOL)
        async def confirm_linear(ctx: RunContext[None]) -> str:
            """Report whether this user's Linear connection has finished."""
            profile = self.bound_profile()
            status = await self.manager.connection_status(profile, self.connector.id)
            if status == "active":
                return (
                    "Linear is connected. Its tools will be available on this user's "
                    "next message."
                )
            if status == "invalid":
                return (
                    "Linear was connected and is not any more — the authorization was "
                    "revoked or expired. Offer to send a fresh link."
                )
            return (
                "Linear is not connected yet. The link finishes the connection by "
                "itself once they approve it; there is nothing to do here but wait "
                "and check again."
            )

        return toolset
