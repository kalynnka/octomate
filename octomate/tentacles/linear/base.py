"""Linear as a tentacle: a person's own workspace over Linear's MCP server,
linked once through the deployment's authorization-code flow.

Nothing but a provider — no platform to front, no turns to run. The configured
key is its id, the connector its tokens live under, and the prefix its tools
carry unless the config names another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from octomate.config.mcp.linear import LinearMcpConfig
from octomate.managers.oauth import OAuthConnector
from octomate.oauth.linear import LinearAuthorizationCodeOAuthFlow
from octomate.schemas.oauth import DirectHttpOAuthCallbackTransport
from octomate.tentacles.mcp import OAuthMcpTentacle

if TYPE_CHECKING:
    from octomate.base import Octomate

LINEAR_INSTRUCTIONS = """\
## Linear

Linear's tools work in the person's own workspace — issues, projects, cycles,
comments — as them, with the access they granted when they linked it.
"""


class LinearTentacle(OAuthMcpTentacle):
    label = "Linear"
    instructions = LINEAR_INSTRUCTIONS

    def __init__(self, id: str, octomate: Octomate, *, config: LinearMcpConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        self.upstream = config.endpoint
        self.prefix = config.prefix or id
        # Registering a direct-HTTP transport is also what makes `Octomate.app`
        # serve the start and callback routes its URIs point at.
        octomate.oauth.register(
            OAuthConnector(
                id=id,
                flow=LinearAuthorizationCodeOAuthFlow(
                    client_id=config.client_id,
                    client_secret=config.client_secret,
                    scopes=config.scopes,
                ),
                callback_transport=DirectHttpOAuthCallbackTransport(
                    config.callback_base_uri
                ),
            )
        )
