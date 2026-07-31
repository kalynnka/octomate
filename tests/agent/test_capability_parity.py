"""The wrapper agent must serve a capability exactly what a plain agent serves it.

`stream_events` drives the graph by hand so it can emit segments as they validate.
Every hook it drives itself is one pydantic-ai would otherwise have driven, and a
capability has no way to tell it is running here rather than on a plain `Agent` —
so anything that works against the native API has to work against this.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.capabilities.abstract import (
    AgentNode,
    NodeResult,
    WrapNodeRunHandler,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.tools import RunContext
from pydantic_graph import End

from octomate.capabilities.harness.agent import Agent


class RecordingCapability(AbstractCapability[None]):
    """Records which hooks fired, and on which node."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def before_node_run(
        self, ctx: RunContext[None], *, node: AgentNode[None]
    ) -> AgentNode[None]:
        self.calls.append(f"before_node_run:{type(node).__name__}")
        return node

    async def wrap_node_run(
        self,
        ctx: RunContext[None],
        *,
        node: AgentNode[None],
        handler: WrapNodeRunHandler[None],
    ) -> NodeResult[None]:
        self.calls.append(f"wrap_node_run:{type(node).__name__}")
        return await handler(node)

    async def after_node_run(
        self,
        ctx: RunContext[None],
        *,
        node: AgentNode[None],
        result: NodeResult[None],
    ) -> NodeResult[None]:
        self.calls.append(f"after_node_run:{type(node).__name__}")
        return result

    async def on_node_run_error(
        self, ctx: RunContext[None], *, node: AgentNode[None], error: Exception
    ) -> NodeResult[None]:
        self.calls.append(f"on_node_run_error:{type(node).__name__}")
        raise error

    def wrap_run_event_stream(
        self, ctx: RunContext[None], *, stream: AsyncIterable[AgentStreamEvent]
    ) -> AsyncIterator[AgentStreamEvent]:
        self.calls.append("wrap_run_event_stream")
        return aiter(stream)


def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    called = any(
        isinstance(part, ToolCallPart) and part.tool_name == "ping"
        for message in messages
        for part in getattr(message, "parts", [])
    )
    if not called:
        return ModelResponse(parts=[ToolCallPart("ping", {})])
    return ModelResponse(parts=[TextPart("pong")])


async def reply_stream(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | DeltaToolCalls]:
    called = any(
        isinstance(part, ToolCallPart) and part.tool_name == "ping"
        for message in messages
        for part in getattr(message, "parts", [])
    )
    if not called:
        yield {0: DeltaToolCall(name="ping", json_args="{}")}
        return
    yield "pong"


def build_agent(capability: RecordingCapability) -> Agent[None, str]:
    agent: Agent[None, str] = Agent(
        FunctionModel(reply, stream_function=reply_stream),
        deps_type=type(None),
        output_type=str,
        capabilities=[capability],
    )

    @agent.tool_plain
    def ping() -> str:
        return "pong"

    return agent


def node_hooks(calls: list[str]) -> list[str]:
    return [call for call in calls if "node_run" in call]


async def test_node_hooks_fire_the_way_a_plain_run_fires_them() -> None:
    """`AgentRun.__anext__` — the obvious way to drive `iter()` — runs none of
    these. A capability built on them must not be able to tell the difference."""

    native = RecordingCapability()
    await build_agent(native).run("go")

    wrapper = RecordingCapability()
    async for _ in build_agent(wrapper).stream_events("go"):
        pass

    assert node_hooks(native.calls), "the plain run should have fired node hooks"
    assert node_hooks(wrapper.calls) == node_hooks(native.calls)


async def test_every_node_gets_the_whole_lifecycle() -> None:
    """Not just the streaming nodes: `UserPromptNode` and the terminal node run
    through the same hooks, because the graph does not exempt them."""

    capability = RecordingCapability()
    async for _ in build_agent(capability).stream_events("go"):
        pass

    for node in ("UserPromptNode", "ModelRequestNode", "CallToolsNode"):
        assert f"before_node_run:{node}" in capability.calls
        assert f"wrap_node_run:{node}" in capability.calls
        assert f"after_node_run:{node}" in capability.calls


async def test_a_node_hook_can_still_wrap_the_streaming() -> None:
    """`before_node_run` fires before the node streams and `after_node_run` after,
    so a capability wrapping a node encloses the events that node produced."""

    capability = RecordingCapability()
    events: list[object] = []
    async for event in build_agent(capability).stream_events("go"):
        if not isinstance(event, AgentRunResultEvent):
            events.append(event)

    assert events, "the run should have streamed something"
    first_model_node = capability.calls.index("before_node_run:ModelRequestNode")
    assert "wrap_run_event_stream" in capability.calls[first_model_node:]


async def test_the_run_still_reaches_a_result() -> None:
    """The hand-driven loop has to terminate on `End` like the graph's own does."""

    capability = RecordingCapability()
    result = None
    async for event in build_agent(capability).stream_events("go"):
        if isinstance(event, AgentRunResultEvent):
            result = event.result

    assert result is not None
    assert result.output == "pong"
    assert not isinstance(result, End)
