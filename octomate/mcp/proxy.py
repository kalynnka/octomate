"""Expose each authorized installation as ordinary MCP tools."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Unpack

import httpx2
from fastmcp.apps.config import UI_EXTENSION_ID, UI_MIME_TYPE
from fastmcp.client.transports.base import (
    ClientTransport,
    SessionKwargs,
    TransportOptions,
)
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.providers import Provider
from fastmcp.server.transforms import GetToolNext, Namespace
from fastmcp.server.transforms.search import RegexSearchTransform
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.base import InputRequiredToolResult
from fastmcp.tools.tool_transform import forward_raw
from fastmcp.utilities.versions import VersionSpec
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import UUID7, JsonValue, ValidationError

from octomate.managers.gateway import OctomateSession
from octomate.managers.mcp import McpManager, McpUnavailable
from octomate.schemas.mcp import Mcp, McpAppToolMeta

logger = logging.getLogger(__name__)
SEARCH_MCP_TOOLS = "mcp_search_tools"


def installation_prefix(namespace: str) -> str:
    """Encode installation names without the underscore separating tool names.

    Local names contain letters, digits, hyphens and underscores but no dots,
    so replacing underscores with dots preserves distinct installation names.
    """
    return namespace.replace("/", "-").replace("_", ".")


class InstalledMcpTransport(ClientTransport):
    """Supply installation credentials while FastMCP owns the proxy connection."""

    def __init__(
        self,
        manager: McpManager,
        resolve_session: Callable[[], Awaitable[OctomateSession]],
        installation_id: UUID7,
    ) -> None:
        self.manager: McpManager = manager
        self.resolve_session: Callable[[], Awaitable[OctomateSession]] = resolve_session
        self.installation_id: UUID7 = installation_id

    @asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Unpack[SessionKwargs],
    ) -> AsyncGenerator[ClientSession]:
        transport = await self.manager.transport(
            await self.resolve_session(), mcp_id=self.installation_id
        )
        # Each installation owns its credentials; never forward the caller's
        # Octomate bearer or other frontend headers to an upstream server.
        options = replace(
            transport_options or TransportOptions(), forward_incoming_headers=False
        )
        session_kwargs["extensions"] = {
            **(session_kwargs.get("extensions") or {}),
            UI_EXTENSION_ID: {"mimeTypes": [UI_MIME_TYPE]},
        }
        async with transport.connect_session(
            transport_options=options, **session_kwargs
        ) as session:
            yield session


class InstalledMcpProvider(Provider):
    """Resolve installations per request; retained tools remain bound to UUIDs."""

    def __init__(
        self,
        manager: McpManager,
        resolve_session: Callable[[], Awaitable[OctomateSession]],
    ) -> None:
        super().__init__()
        self.manager: McpManager = manager
        self.resolve_session: Callable[[], Awaitable[OctomateSession]] = resolve_session

    async def installations(self, scope: OctomateSession) -> list[Mcp]:
        if scope.user_profile is None:
            return []
        owner = await self.manager.users.owner(scope.user_profile)
        if owner is None:
            return []
        return await self.manager.list(owner.id, enabled=True)

    def proxy_tool(self, instance: Mcp, proxy: Tool) -> Tool:
        tool = proxy.to_mcp_tool()

        async def invoke(**arguments: JsonValue) -> ToolResult:
            try:
                scope = await self.resolve_session()
                user, current = await self.manager.resolve(scope, mcp_id=instance.id)
                ui = McpAppToolMeta.model_validate(tool.meta or {})
                result = await forward_raw(**arguments)
                if isinstance(result, InputRequiredToolResult):
                    return result
                captured = await self.manager.capture_app(
                    scope,
                    user,
                    current,
                    tool,
                    ui,
                    arguments,
                    CallToolResult(
                        content=result.content,
                        structured_content=result.structured_content,
                        is_error=result.is_error,
                        _meta=result.meta,
                    ),
                )
                return result.model_copy(update={"meta": captured.meta})
            except (McpUnavailable, ValidationError) as error:
                raise ToolError(str(error)) from error
            except (httpx2.HTTPError, MCPError, TimeoutError) as error:
                raise ToolError("MCP upstream request failed") from error

        return Tool.from_tool(
            proxy, transform_fn=invoke, output_schema=proxy.output_schema
        )

    async def get_tasks(self) -> list[Tool]:
        """Proxy tools have no local background tasks or startup-time identity."""
        return []

    async def _list_tools(self) -> list[Tool]:
        tools: list[Tool] = []
        scope = await self.resolve_session()
        for instance in await self.installations(scope):
            proxy = create_proxy(
                InstalledMcpTransport(self.manager, self.resolve_session, instance.id),
                provider_error_strategy="raise",
            )
            try:
                upstream_tools = await proxy.list_tools()
            except McpUnavailable:
                continue
            except (httpx2.HTTPError, MCPError, TimeoutError, RuntimeError):
                logger.warning("Could not discover MCP installation %s", instance.id)
                continue
            namespace = Namespace(installation_prefix(instance.namespace))
            tools.extend(
                await namespace.list_tools(
                    [self.proxy_tool(instance, tool) for tool in upstream_tools]
                )
            )
        return tools

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> Tool | None:
        prefix, separator, upstream_name = name.partition("_")
        if not separator or not prefix.startswith("personal-"):
            return None
        scope = await self.resolve_session()
        for instance in await self.installations(scope):
            if installation_prefix(instance.namespace) != prefix:
                continue
            proxy = create_proxy(
                InstalledMcpTransport(self.manager, self.resolve_session, instance.id),
                provider_error_strategy="raise",
            )
            upstream = await proxy.get_tool(upstream_name, version=version)
            if upstream is None:
                return None
            tool = self.proxy_tool(instance, upstream)
            return tool.model_copy(update={"name": name})
        return None


class ToolSearch(RegexSearchTransform):
    """Add FastMCP's search without hiding tools or adding an invocation tool."""

    def __init__(self) -> None:
        super().__init__(search_tool_name=SEARCH_MCP_TOOLS)

    def _make_search_tool(self) -> Tool:
        return (
            super()
            ._make_search_tool()
            .model_copy(update={"annotations": ToolAnnotations(read_only_hint=True)})
        )

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [*tools, self._make_search_tool()]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        if name == SEARCH_MCP_TOOLS:
            return self._make_search_tool()
        return await call_next(name, version=version)
