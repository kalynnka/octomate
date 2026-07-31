from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import FunctionToolset

from octomate.capabilities.harness.events import OAuthDeviceAuthorizationEvent
from octomate.capabilities.harness.mcp import OAuthMcpCapability
from octomate.config import GitHubMcpConfig
from octomate.config.mcp import McpIntegrationConfig
from octomate.managers.oauth import NoPendingAuthorization
from octomate.schemas.oauth import (
    DeviceAuthorization,
    DeviceOAuthFlow,
    OAuthPending,
)

# How a channel names this integration when it presents its authorization.
GITHUB_LABEL = "GitHub"

GITHUB_CONNECT_TOOL = "connect_github"
GITHUB_CONFIRM_TOOL = "confirm_github"

# The catalog line the model reads while this capability is deferred — all it knows
# about GitHub until it loads it, so it names both the work and the connecting.
GITHUB_CAPABILITY_DESCRIPTION = (
    "Work with this user's own GitHub account — repositories, issues, pull requests "
    "— and connect that account if they have not linked it yet."
)

GITHUB_OAUTH_INSTRUCTION = """\
## GitHub connection

GitHub tools are unavailable until this user connects their own account.
- Call `connect_github` when the user asks to connect GitHub or needs a GitHub tool.
- It sends the verification link and code to this user's direct messages — never to
  a group, so point them there rather than saying it is in this conversation. You are
  not given the link or the code and cannot repeat them.
- After the user says they authorized it, call `confirm_github` once. If connected,
  GitHub tools become available on their next message.
"""

GITHUB_RETIRED_INSTRUCTION = """\
## GitHub connection

This user WAS connected to GitHub and no longer is: GitHub rejected their
authorization, so it was revoked, expired, or had access withdrawn. Their GitHub
tools are gone until they connect again, and nothing has told them.
- Say so once in your next reply, briefly, even if they asked about something else
  — they cannot see this and may be waiting on work that can no longer happen.
- Offer to reconnect. Call `connect_github` when they agree; it sends a fresh
  verification link and code to their direct messages, never to a group. You are not
  given the link or the code and cannot repeat them.
- After the user says they authorized it, call `confirm_github` once. If connected,
  GitHub tools become available on their next message.
"""

logger = logging.getLogger(__name__)


@dataclass
class GitHubCapability(OAuthMcpCapability):
    """GitHub's device authorization over the shared per-user OAuth/MCP capability."""

    mcp_config: McpIntegrationConfig = field(default_factory=GitHubMcpConfig)

    label = GITHUB_LABEL
    connect_tool = GITHUB_CONNECT_TOOL
    confirm_tool = GITHUB_CONFIRM_TOOL
    capability_description = GITHUB_CAPABILITY_DESCRIPTION
    oauth_instruction = GITHUB_OAUTH_INSTRUCTION
    retired_instruction = GITHUB_RETIRED_INSTRUCTION
    flow_type = DeviceOAuthFlow

    def build_oauth_toolset(self) -> FunctionToolset[None]:
        toolset: FunctionToolset[None] = FunctionToolset(id="github-oauth")

        @toolset.tool(name=GITHUB_CONNECT_TOOL)
        async def connect_github(ctx: RunContext[None]) -> ToolReturn[str]:
            """Send this user GitHub's device authorization link and one-time code."""
            authorization = await self.manager.start(
                self.bound_profile(),
                self.connector.id,
            )
            if not isinstance(authorization, DeviceAuthorization):
                raise ValueError("GitHub is not configured for device OAuth")
            # The link and code go to the channel as an authorization of its own, for
            # the channel to present — never through this return value, which the model
            # reads and could repeat into a reply.
            return ToolReturn(
                return_value=(
                    "The authorization link and code are on their way to this user's "
                    "direct messages."
                ),
                metadata=[
                    OAuthDeviceAuthorizationEvent(
                        connector_id=self.connector.id,
                        label=GITHUB_LABEL,
                        authorization_uri=str(
                            authorization.verification_uri_complete
                            or authorization.verification_uri
                        ),
                        user_code=authorization.user_code.get_secret_value(),
                    )
                ],
            )

        @toolset.tool(name=GITHUB_CONFIRM_TOOL)
        async def confirm_github(ctx: RunContext[None]) -> str:
            """Confirm that this user authorized their pending GitHub connection."""
            try:
                result = await self.manager.complete_latest(
                    self.bound_profile(),
                    self.connector.id,
                )
            except NoPendingAuthorization:
                # Confirming before connecting: an ordering the model can fix
                # itself, so say which tool opens one rather than ending the turn.
                raise ModelRetry(
                    f"Nothing to confirm — this user has no GitHub authorization "
                    f"waiting. Call `{GITHUB_CONNECT_TOOL}` to start one, then "
                    "confirm once they have entered the code."
                ) from None
            if isinstance(result, OAuthPending):
                return (
                    "GitHub is still waiting for authorization. They have to finish "
                    "it from the link in their direct messages, then try again in "
                    f"{result.retry_after_seconds} seconds."
                )
            return (
                f"GitHub connected as @{result.account_label}. GitHub tools will be "
                "available on this user's next message."
            )

        return toolset
