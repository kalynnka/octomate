"""GitHub as a tentacle: a person's own account over GitHub's MCP server, linked
once through GitHub's device flow — a link and a code, no callback.

Nothing but a provider — no platform to front, no turns to run. The configured
key is its id, the connector its tokens live under, and the prefix its tools
carry unless the config names another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from octomate.config.mcp.github import GitHubMcpConfig
from octomate.managers.oauth import OAuthConnector
from octomate.oauth.github import GitHubDeviceOAuthFlow
from octomate.tentacles.mcp import OAuthMcpTentacle

if TYPE_CHECKING:
    from octomate.base import Octomate

GITHUB_INSTRUCTIONS = """\
## GitHub

GitHub's tools work on the person's own account — repositories, issues, pull
requests — as them, with the access they granted when they linked it.
"""


class GitHubTentacle(OAuthMcpTentacle):
    label = "GitHub"
    instructions = GITHUB_INSTRUCTIONS

    def __init__(self, id: str, octomate: Octomate, *, config: GitHubMcpConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        self.upstream = config.endpoint
        self.prefix = config.prefix or id
        octomate.oauth.register(
            OAuthConnector(
                id=id,
                flow=GitHubDeviceOAuthFlow(
                    client_id=config.client_id, scopes=config.scopes
                ),
            )
        )
