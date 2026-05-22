"""Unit tests for the Inkling ReAct loop with deferred-tool resolution.

Drives the graph end-to-end against a scripted FunctionModel (no real LLM call)
to prove:
- `CallDeferred` from `ask_user` round-trips through the loop
- streaming events from `agent.run_stream_events` come from the graph output
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.tools import DeferredToolRequests

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.tentacles.agent.inkling import (
    InklingTentacle,
    InklingDeps,
    InklingState,
    StartTurn,
    StubResolver,
    inkling_graph,
    inkling_toolset,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT

InklingTestOutput = str | DeferredToolRequests
InklingTestEvent = AgentStreamEvent | AgentRunResultEvent[InklingTestOutput]


@dataclass
class ScriptedTurn:
    """One turn of the conversation: one tool call to emit as a streamed delta."""

    tool_name: str
    args: dict[str, object]
    tool_call_id: str


@dataclass
class ScriptedStream:
    """FunctionModel stream callback: emits tool-call deltas or text output."""

    turns: list[ScriptedTurn | str]
    cursor: int = 0
    seen_output_tools: list[list[str]] = field(default_factory=list)
    __name__: str = "scripted_stream"

    def __call__(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        self.seen_output_tools.append([t.name for t in info.output_tools])
        turn = self.turns[self.cursor]
        self.cursor += 1
        return _emit_scripted_turn(turn)


@dataclass
class FakeConversationManager(ConversationManager):
    conversation: Conversation = field(default_factory=lambda: _test_conversation())
    runs: list[tuple[Conversation, str, list[ModelMessage]]] = field(
        default_factory=list
    )
    discarded: list[ModelResponse] = field(default_factory=list)

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str | None = None,
    ) -> Conversation:
        self.conversation.agent_tentacle_id = agent_tentacle_id
        return self.conversation

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[ModelMessage],
    ) -> None:
        self.runs.append((conversation, run_id, list(messages)))

    async def discard_message(self, message: ModelResponse) -> None:
        self.discarded.append(message)


async def _emit_scripted_turn(
    turn: ScriptedTurn | str,
) -> AsyncIterator[str | DeltaToolCalls]:
    if isinstance(turn, str):
        yield turn
        return
    yield {
        0: DeltaToolCall(
            name=turn.tool_name,
            json_args=json.dumps(turn.args),
            tool_call_id=turn.tool_call_id,
        )
    }


def _build_test_agent(
    turns: list[ScriptedTurn | str],
) -> tuple[Agent[None, InklingTestOutput], ScriptedStream]:
    script = ScriptedStream(turns=turns)
    agent: Agent[None, InklingTestOutput] = Agent(
        FunctionModel(stream_function=script, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
    return agent, script


def _build_non_stream_agent() -> Agent[None, InklingTestOutput]:
    def respond(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="all done!")])

    return Agent(
        FunctionModel(function=respond, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )


def _test_conversation() -> Conversation:
    return Conversation(
        chat_type="private",
        chat_id="test",
        user_id="test",
        channel_tentacle_id="test",
    )


def _test_conversation_key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="test",
        chat_type="private",
        chat_id="test",
        user_id="test",
    )


async def test_inkling_loop_resolves_deferred_calls() -> None:
    agent, script = _build_test_agent(
        [
            ScriptedTurn(
                tool_name="ask_user",
                args={"question": "what's your name?"},
                tool_call_id="call_ask_1",
            ),
            "all done!",
        ]
    )

    captured_events: list[InklingTestEvent] = []
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(agent=agent, conversation_manager=conversations)
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_key=_test_conversation_key(),
    ) as stream:
        async for event in stream:
            captured_events.append(event)

    result_events = [
        event for event in captured_events if isinstance(event, AgentRunResultEvent)
    ]
    assert len(result_events) == 2
    result = result_events[-1].result

    output = result.output
    assert output == "all done!"

    assert captured_events, "graph output should stream pydantic events"

    assert script.cursor == 2, "both turns should have been consumed"
    assert len(conversations.runs) == 2


async def test_inkling_tentacle_stream_events_forwards_graph_events() -> None:
    agent, script = _build_test_agent(
        [
            "all done!",
        ]
    )
    tentacle = InklingTentacle(
        agent=agent,
        conversation_manager=FakeConversationManager(),
    )

    captured_events: list[InklingTestEvent] = []
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_key=_test_conversation_key(),
    ) as stream:
        async for event in stream:
            captured_events.append(event)

    result_events = [
        event for event in captured_events if isinstance(event, AgentRunResultEvent)
    ]
    assert result_events
    assert result_events[-1].result.output == "all done!"
    assert script.cursor == 1


async def test_inkling_loop_handles_immediate_final_response() -> None:
    """If the model finalizes immediately, the loop ends after one RunAgent step."""

    agent = _build_non_stream_agent()

    deps = InklingDeps(
        agent=agent,
        conversation_manager=FakeConversationManager(),
    )

    result = await inkling_graph.run(
        StartTurn(user_prompt="just say done"),
        state=InklingState(conversation=_test_conversation()),
        deps=deps,
    )

    assert result.output.output == "all done!"


async def test_stub_resolver_round_trips_calls_and_approvals() -> None:
    """Direct unit-style check: calls get canned strings, approvals get bools."""

    requests = DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_user", args={"question": "?"}, tool_call_id="c1"
            )
        ],
        approvals=[
            ToolCallPart(
                tool_name="confirm_action",
                args={"action": "archive"},
                tool_call_id="a1",
            )
        ],
    )
    resolver = StubResolver(canned={"ask_user": "yes please"})
    results = await resolver.resolve(requests)

    assert results.calls["c1"] == "yes please"
    assert results.approvals["a1"] is True
