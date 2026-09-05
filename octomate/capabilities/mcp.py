"""Octomate's MCP tentacles as an Inkling capability: the tools the served
server lists a turn, mounted in process for one run.

Inkling runs in this process, so nothing goes over the wire — not even in
memory: the tentacles' server is called as the object it is, no client and no
transport between them. The run's gateway session is closed over, every
tentacle lists and calls as the person that session is for, and the linking
pair is there beside them. The spells and the history Inkling has as
capabilities of its own, so what it mounts here is the tentacles' server
alone, deferred: named by a catalog line, its tools flagged for the model to
discover or load before they are on the wire, so a listing that differs per
person never touches the prompt prefix.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from octomate.managers.gateway import GatewaySession
from octomate.mcp.server import tentacles_mcp
from octomate.tentacles.mcp import McpTentacle

# The upstream validates a call's arguments itself, as the served proxy lets it;
# nothing is checked twice.
ANY_ARGUMENTS = SchemaValidator(core_schema.any_schema())


class TentaclesToolset(AbstractToolset[None]):
    """A FastMCP server's tools as a pydantic-ai toolset, the server called in
    process as the object it is. Named as the server is, and its instructions
    are the server's own."""

    def __init__(self, server: FastMCP) -> None:
        self.server = server

    @property
    def id(self) -> str | None:
        return self.server.name

    async def get_instructions(self, ctx: RunContext[None]) -> str | None:
        return self.server.instructions

    async def get_tools(self, ctx: RunContext[None]) -> dict[str, ToolsetTool[None]]:
        return {
            tool.name: ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.parameters,
                ),
                max_retries=ctx.max_retries,
                args_validator=ANY_ARGUMENTS,
            )
            for tool in await self.server.list_tools()
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> dict[str, Any] | str:
        try:
            result = await self.server.call_tool(name, tool_args)
        except ToolError as refusal:
            # The same corrective sentence every runtime reads, as the retry
            # Inkling corrects from.
            raise ModelRetry(str(refusal)) from refusal
        # What the upstream said, as it said it: text when it spoke in text —
        # FastMCP wraps a bare value as `{"result": …}` beside it, which is not
        # the upstream's — and its structured answer otherwise.
        texts = [
            block.text for block in result.content if isinstance(block, TextContent)
        ]
        if len(texts) == len(result.content):
            return "\n".join(texts)
        if result.structured_content is not None:
            return result.structured_content
        return "\n".join(str(block) for block in result.content)


def tentacles_capability(
    session: GatewaySession, tentacles: Sequence[McpTentacle]
) -> Toolset[None]:
    """The capability a run mounts: the tentacles' server built over `session`,
    deferred behind a catalog line naming what it holds."""

    async def fixed() -> GatewaySession:
        return session

    server = tentacles_mcp(fixed, tentacles)
    labels = ", ".join(dict.fromkeys(tentacle.label for tentacle in tentacles))
    return Toolset(
        TentaclesToolset(server),
        id=server.name,
        description=(
            f"The tools of {labels}, acting as the person you are answering, and "
            "the linking of their accounts."
        ),
        defer_loading=True,
    )
