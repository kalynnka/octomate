from __future__ import annotations

from octomate.capabilities.github import GitHubCapability
from octomate.capabilities.harness.mcp import OAuthMcpCapability
from octomate.capabilities.linear import LinearCapability
from octomate.config.integrations import (
    GitHubIntegrationConfig,
    IntegrationConfig,
    LinearIntegrationConfig,
)
from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.oauth.github import GitHubDeviceOAuthFlow
from octomate.oauth.linear import LinearAuthorizationCodeOAuthFlow
from octomate.schemas.oauth import DirectHttpOAuthCallbackTransport


def build_integration(
    name: str,
    integration: IntegrationConfig,
    manager: OAuthManager,
) -> OAuthMcpCapability:
    """Compose one configured integration into its connector and its capability.

    The one place a `type:` becomes a provider. Everything below the connector is
    provider-neutral — the manager, the capability base, the routes — so a new
    integration is a config variant and a branch here, and nothing else learns its
    name. The configured key is the connector id throughout, which is what lets one
    vendor be mounted more than once, a key per account.
    """
    match integration:
        case GitHubIntegrationConfig():
            connector = manager.register(
                OAuthConnector(
                    id=name,
                    flow=GitHubDeviceOAuthFlow(
                        client_id=integration.client_id,
                        scopes=integration.scopes,
                    ),
                )
            )
            return GitHubCapability(
                manager=manager,
                connector=connector,
                mcp_config=integration.mcp,
                max_cached_users=integration.max_cached_users,
                id=name,
                defer_loading=True,
            )
        case LinearIntegrationConfig():
            connector = manager.register(
                OAuthConnector(
                    id=name,
                    flow=LinearAuthorizationCodeOAuthFlow(
                        client_id=integration.client_id,
                        client_secret=integration.client_secret,
                        scopes=integration.scopes,
                    ),
                    # Registering this transport is also what makes `Octomate.app`
                    # serve the start and callback routes its URIs point at.
                    callback_transport=DirectHttpOAuthCallbackTransport(
                        integration.callback_base_uri
                    ),
                )
            )
            return LinearCapability(
                manager=manager,
                connector=connector,
                mcp_config=integration.mcp,
                max_cached_users=integration.max_cached_users,
                id=name,
                defer_loading=True,
            )
