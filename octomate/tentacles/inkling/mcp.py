"""Inkling's core tools over the authenticated MCP endpoint."""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from typing import cast

from octomate_protocol.gateway import GatewayTool, gateway_tool
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import AgentCapability, AgentStreamEvent, CallDeferred, RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.mcp import CallToolFunc, MCPToolset
from pydantic_ai.mcp import ToolResult as McpToolResult
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
)
from pydantic_ai.toolsets import ApprovalRequiredToolset

from octomate.capabilities.harness.events import (
    SubagentActivityStatus,
    SubagentSettledEvent,
    SubagentStartedEvent,
)
from octomate.capabilities.history import HISTORY_TOOLS, history_instructions
from octomate.mcp.gateway import (
    HANDOFF_INSTRUCTION,
    gateway_instructions,
)
from octomate.mcp.server import history_tool, tentacle_instructions
from octomate.mcp.todos import TODO_TOOL_NAMES, todo_instructions
from octomate.schemas.triage import TELEPORT_DEFER_KIND
from octomate.types.json import JsonObject

json_object_adapter = TypeAdapter(JsonObject)


@dataclass
class Accomplices:
    """The accomplices one stretch of a run called and has not heard back from."""

    working: list[str] = field(default_factory=list)

    async def watch(
        self, stream: AsyncIterable[AgentStreamEvent]
    ) -> AsyncIterator[AgentStreamEvent]:
        """`stream` with each accomplice's start before its call and its finish
        before its result. One left unanswered finishes with the stretch: cancelled
        with it, or failed."""
        try:
            async for event in stream:
                lifecycle = self.observe(event)
                if lifecycle is not None:
                    # One dynamic-boundary cast: pydantic-ai types the stream as
                    # AgentStreamEvent, consumers match the concrete octomate event.
                    yield cast(AgentStreamEvent, lifecycle)
                yield event
        except (Exception, asyncio.CancelledError) as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            for settled in self.abandon("cancelled" if cancelled else "failed"):
                yield settled
            raise
        for settled in self.abandon("failed"):
            yield settled

    def observe(
        self, event: AgentStreamEvent
    ) -> SubagentStartedEvent | SubagentSettledEvent | None:
        if isinstance(event, FunctionToolResultEvent):
            part = event.part
            if part.tool_call_id not in self.working:
                return None
            self.working.remove(part.tool_call_id)
            if isinstance(part, RetryPromptPart):
                return SubagentSettledEvent(
                    invocation_id=part.tool_call_id,
                    status="failed",
                    response=part.model_response(),
                )
            return SubagentSettledEvent(
                invocation_id=part.tool_call_id,
                status="completed" if part.outcome == "success" else "failed",
                response=part.model_response_str(),
            )
        if not isinstance(event, FunctionToolCallEvent) or event.part.tool_name not in {
            GatewayTool.COMMISSION,
            GatewayTool.WHISPER,
        }:
            return None
        name = event.part.args_as_dict().get("name")
        if not isinstance(name, str) or not name:
            # The spell refuses a nameless call, so no accomplice starts.
            return None
        self.working.append(event.part.tool_call_id)
        return SubagentStartedEvent(
            invocation_id=event.part.tool_call_id,
            kind="commission"
            if event.part.tool_name == GatewayTool.COMMISSION
            else "whisper",
            name=name,
        )

    def abandon(self, status: SubagentActivityStatus) -> list[AgentStreamEvent]:
        settled = [
            cast(
                AgentStreamEvent,
                SubagentSettledEvent(invocation_id=invocation_id, status=status),
            )
            for invocation_id in self.working
        ]
        self.working.clear()
        return settled


class OctomateTools(Toolset[None]):
    """Preserve Inkling's approval, deferral and subagent display behavior over MCP."""

    def get_instructions(self) -> str:
        return "\n".join(
            (
                gateway_instructions(lambda name: name),
                HANDOFF_INSTRUCTION,
                history_instructions(lambda name: name),
                todo_instructions(),
                tentacle_instructions(),
            )
        )

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[None],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        async for event in Accomplices().watch(stream):
            yield event

    @staticmethod
    async def call(
        ctx: RunContext[None],
        call_tool: CallToolFunc,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> McpToolResult:
        result = await call_tool(
            name,
            arguments,
            metadata={
                "tool_call_id": ctx.tool_call_id,
                "defer_teleport": name == gateway_tool(GatewayTool.TELEPORT),
            },
        )
        if name == gateway_tool(GatewayTool.TELEPORT):
            metadata = json_object_adapter.validate_python(result)
            if metadata.get("kind") != TELEPORT_DEFER_KIND:
                raise ValueError("The MCP gateway did not return a teleport deferral")
            raise CallDeferred(metadata=metadata)
        return result

    @classmethod
    def capabilities(cls, client: MCPToolset[None]) -> list[AgentCapability[None]]:
        """Keep the existing tool names and deferred discovery groups."""
        names = {
            **{name: gateway_tool(name) for name in GatewayTool},
            **{name: history_tool(name) for name in HISTORY_TOOLS},
        }
        tools = client.renamed(names)
        gateway = tools.filtered(lambda ctx, tool: tool.name in GatewayTool)
        return [
            cls(
                ApprovalRequiredToolset(
                    gateway,
                    approval_required_func=lambda ctx, tool, arguments: (
                        tool.name == GatewayTool.TELEPORT
                    ),
                ),
                id="gateway",
            ),
            Toolset(
                tools.filtered(lambda ctx, tool: tool.name in TODO_TOOL_NAMES),
                id="todos",
                description="Persisted task list for planning and tracking multi-step work.",
                defer_loading=True,
            ),
            Toolset(
                tools.filtered(lambda ctx, tool: tool.name in HISTORY_TOOLS),
                id="history",
                description="Search and page the current user's conversation history.",
                defer_loading=True,
            ),
            Toolset(
                tools.filtered(
                    lambda ctx, tool: tool.name not in {*names, *TODO_TOOL_NAMES}
                ),
                id="tentacles",
                description="The user's installed MCP tools and account linking.",
                defer_loading=True,
            ),
        ]
