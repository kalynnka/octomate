"""Configured MCP declarations from which users create their own MCPs."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Literal

from pydantic import SecretStr

from octomate.config.mcp import (
    BareMcpConfig,
    McpConfigVariant,
    OAuthMcpConfig,
)
from octomate.config.mcp.base import AuthorizationCodeFlowConfig, DeviceFlowConfig
from octomate.managers.oauth import OAuthConnector
from octomate.oauth.mcp import McpDeviceOAuthFlow, McpOAuthFlow, OAuthTokenExchange
from octomate.schemas.mcp import McpTentacleInfo
from octomate.schemas.oauth import DirectHttpOAuthCallbackTransport
from octomate.tentacles.base import Tentacle

if TYPE_CHECKING:
    from octomate.base import Octomate


class McpTentacle(Tentacle):
    """Connection settings and tool instructions offered for installation."""

    auth_kind: Literal["none", "bearer", "oauth"] = "none"
    token: SecretStr | None = None
    label: str
    upstream: str
    instructions: str

    @cached_property
    def info(self) -> McpTentacleInfo:
        return McpTentacleInfo(
            id=self.id,
            name=self.label,
            url=self.upstream,
            auth_kind=self.auth_kind,
        )

    @property
    def serving(self) -> bool:
        """Whether this tentacle's tools are served at all; a channel may say no."""
        return True


class OAuthMcpTentacle(McpTentacle):
    """An MCP backed by a configured OAuth application."""

    auth_kind = "oauth"


class BareMcpTentacle(McpTentacle):
    """An MCP offered with an optional operator bearer token."""

    @property
    def log_names(self) -> tuple[str, ...]:
        return (__name__,)

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        url: str,
        token: SecretStr | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.label = id
        self.upstream = url
        self.instructions = ""
        self.token = token
        self.auth_kind = "bearer" if token is not None else "none"


def build_mcp(id: str, config: McpConfigVariant, octomate: Octomate) -> McpTentacle:
    """Build a tentacle and register any configured OAuth application."""
    match config:
        case BareMcpConfig():
            return BareMcpTentacle(id, octomate, url=config.url, token=config.token)
        case OAuthMcpConfig():
            tokens = OAuthTokenExchange(
                token_endpoint=config.flow.token_endpoint,
                client_id=config.client_id,
                client_secret=config.client_secret,
                token_endpoint_auth_method=config.token_endpoint_auth_method,
                scopes=config.scopes,
                scope_separator=config.scope_separator,
                invalid_credentials_errors=config.invalid_credentials_errors,
                httpx_client_factory=octomate.oauth.httpx_client_factory,
            )
            match config.flow:
                case DeviceFlowConfig():
                    flow = McpDeviceOAuthFlow(
                        device_authorization_endpoint=config.flow.device_authorization_endpoint,
                        tokens=tokens,
                    )
                    callback = None
                case AuthorizationCodeFlowConfig():
                    if octomate.oauth.callback_base_uri is None:
                        raise ValueError(
                            "Configure oauth.callback_base_uri for authorization-code MCPs"
                        )
                    flow = McpOAuthFlow(
                        url=config.url,
                        authorization_endpoint=config.flow.authorization_endpoint,
                        tokens=tokens,
                        httpx_client_factory=octomate.oauth.httpx_client_factory,
                    )
                    callback = DirectHttpOAuthCallbackTransport(
                        octomate.oauth.callback_base_uri
                    )
            octomate.oauth.register(
                OAuthConnector(
                    id=id, mcp_url=config.url, flow=flow, callback_transport=callback
                )
            )
            tentacle = OAuthMcpTentacle(id=id, octomate=octomate)
            tentacle.label = id
            tentacle.upstream = str(config.url)
            tentacle.instructions = ""
            return tentacle
