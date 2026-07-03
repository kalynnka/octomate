from __future__ import annotations

from typing import Any

import pytest
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import SecretStr
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import (
    AbstractToolset,
    DeferredLoadingToolset,
    FunctionToolset,
    PrefixedToolset,
)

from octomate import Octomate
from octomate.capabilities.agent import Agent
from octomate.config import GitHubMcpConfig, LinearMcpConfig, McpConfig
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agent.inkling import InklingTentacle, build_mcp_toolsets
from octomate.tentacles.agent.inkling.base import InklingOutput


def _prefixed(toolset: AbstractToolset[None]) -> PrefixedToolset[None]:
    # Each server is wrapped DeferredLoadingToolset -> PrefixedToolset -> MCPToolset.
    assert isinstance(toolset, DeferredLoadingToolset)
    prefixed = toolset.wrapped
    assert isinstance(prefixed, PrefixedToolset)
    return prefixed


def _toolset(toolset: AbstractToolset[None]) -> MCPToolset:
    inner = _prefixed(toolset).wrapped
    assert isinstance(inner, MCPToolset)
    return inner


def _transport(toolset: AbstractToolset[None]) -> StreamableHttpTransport:
    transport = _toolset(toolset).client.transport
    assert isinstance(transport, StreamableHttpTransport)
    return transport


def test_no_servers_returns_empty() -> None:
    assert build_mcp_toolsets(McpConfig()) == []


def test_disabled_server_is_skipped() -> None:
    config = McpConfig(github=GitHubMcpConfig(enabled=False, token=SecretStr("ghp_x")))
    assert build_mcp_toolsets(config) == []


def test_enabled_without_token_fails_fast() -> None:
    config = McpConfig(github=GitHubMcpConfig(enabled=True))
    with pytest.raises(ValueError, match="mcp.github.enabled but no token set"):
        build_mcp_toolsets(config)


def test_github_toolset_is_deferred_with_bearer_header() -> None:
    config = McpConfig(github=GitHubMcpConfig(enabled=True, token=SecretStr("ghp_x")))

    (toolset,) = build_mcp_toolsets(config)

    assert _toolset(toolset).id == "github"
    assert _prefixed(toolset).prefix == "github"
    transport = _transport(toolset)
    assert transport.url == "https://api.githubcopilot.com/mcp/"
    assert transport.headers == {"Authorization": "Bearer ghp_x"}


def test_github_read_only_selects_readonly_endpoint() -> None:
    config = McpConfig(
        github=GitHubMcpConfig(enabled=True, token=SecretStr("ghp_x"), read_only=True)
    )

    (toolset,) = build_mcp_toolsets(config)

    assert _transport(toolset).url == "https://api.githubcopilot.com/mcp/readonly"


def test_linear_toolset_uses_bearer_authorization_header() -> None:
    config = McpConfig(linear=LinearMcpConfig(enabled=True, token=SecretStr("lin_x")))

    (toolset,) = build_mcp_toolsets(config)

    assert _toolset(toolset).id == "linear"
    assert _prefixed(toolset).prefix == "linear"
    assert _transport(toolset).headers == {"Authorization": "Bearer lin_x"}


def test_both_servers_build_in_order() -> None:
    config = McpConfig(
        github=GitHubMcpConfig(enabled=True, token=SecretStr("ghp_x")),
        linear=LinearMcpConfig(enabled=True, token=SecretStr("lin_x")),
    )

    toolsets = build_mcp_toolsets(config)

    assert [_toolset(ts).id for ts in toolsets] == ["github", "linear"]


class SpyToolset(FunctionToolset[None]):
    """Counts how many times the agent enters/exits it, standing in for an MCP
    server's warm session without a real connection."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> SpyToolset:
        self.entered += 1
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        self.exited += 1
        return None


class FailingToolset(FunctionToolset[None]):
    """Stands in for a remote MCP server whose session fails to `initialize`,
    raising when the agent tries to open it during warm-up."""

    async def __aenter__(self) -> FailingToolset:
        raise RuntimeError("Failed to initialize server session")

    async def __aexit__(self, *args: Any) -> bool | None:
        return None


def _tentacle_with(toolset: AbstractToolset[None]) -> InklingTentacle:
    octomate = Octomate()
    agent: Agent[None, InklingOutput] = Agent(
        TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        toolsets=[toolset],
    )
    return InklingTentacle("inkling", octomate, agent=agent)


async def test_entering_tentacle_enters_agent_toolsets_once() -> None:
    spy = SpyToolset()
    tentacle = _tentacle_with(spy)

    async with tentacle:
        assert (spy.entered, spy.exited) == (1, 0)

    assert (spy.entered, spy.exited) == (1, 1)


async def test_warm_up_failure_does_not_abort_startup() -> None:
    tentacle = _tentacle_with(FailingToolset())

    # A transient MCP `initialize` failure while warming must not propagate out of
    # tentacle startup; the agent is left unentered so runs reconnect on demand.
    async with tentacle:
        pass
