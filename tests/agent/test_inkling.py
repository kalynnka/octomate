"""InklingTentacle run entrypoints driving the real react graph against
scripted FunctionModels (no real LLM call)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TypeAlias, cast

import pytest
from pydantic_ai import AgentRunResultEvent, ToolDenied
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.capabilities.harness.deferred import DeclineResolver
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.capabilities.send import SendCapability
from octomate.capabilities.todos import TodoCapability
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment, Segment
from octomate.tentacles.agent.inkling import (
    InklingTentacle,
)
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from tests.support.agents import (
    ScriptedOutput,
    ScriptedTurn,
    build_scripted_agent,
)
from tests.support.managers import FakeConversationManager
from uuid_utils.compat import uuid7

InklingTestEvent: TypeAlias = ReactStreamEvent[ScriptedOutput]


def _inkling_agent() -> Agent[None, InklingOutput]:
    return Agent(
        TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        capabilities=[AskCapability(), TodoCapability(), SendCapability()],
        system_prompt=SYSTEM_PROMPT,
    )


@dataclass
class StubSuspender:
    suspended: list[DeferredToolRequests] = field(default_factory=list)

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        self.suspended.append(requests)
        return None


_THREAD = uuid7()


def _test_conversation_address() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="test",
        chat_type="private",
        chat_id="test",
        user_id="test",
    )


# The loop tests run str-output agents through the graph (they exercise loop
# mechanics, not inkling's reply contract), so the calls below pin output_type
# back to text explicitly.
STR_OUTPUT: list[type[str] | type[DeferredToolRequests]] = [str, DeferredToolRequests]


def _tentacle(
    agent: Agent[None, ScriptedOutput],
    conversations: FakeConversationManager,
) -> InklingTentacle:
    return InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=cast(Agent[None, InklingOutput], agent),
        conversation_manager=conversations,
    )


def _boom_agent() -> Agent[None, ScriptedOutput]:
    async def boom(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise RuntimeError("model boom")
        yield ""  # pragma: no cover - marks this an async generator

    return Agent(
        FunctionModel(stream_function=boom, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        capabilities=[AskCapability()],
        system_prompt=SYSTEM_PROMPT,
    )


async def test_inkling_loop_emits_deferred_question_batch() -> None:
    agent, script = build_scripted_agent(
        [
            ScriptedTurn(
                tool_name="ask_questions",
                args={
                    "questions": [
                        {
                            "question": "what's your name?",
                            "choices": ["Ada", "Grace"],
                            "hint": "Pick or type the name to use.",
                        },
                    ]
                },
                tool_call_id="call_ask_1",
            ),
        ]
    )

    captured_events: list[InklingTestEvent] = []
    conversations = FakeConversationManager()
    tentacle = _tentacle(agent, conversations)
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    ) as stream:
        async for event in stream:
            captured_events.append(event)

    result_events = [
        event for event in captured_events if isinstance(event, AgentRunResultEvent)
    ]
    assert len(result_events) == 1

    output = result_events[-1].result.output
    assert isinstance(output, DeferredToolRequests)
    assert len(output.calls) == 1
    call = output.calls[0]
    assert call.tool_name == "ask_questions"
    assert call.args_as_dict()["questions"][0]["question"] == "what's your name?"

    assert captured_events, "graph output should stream pydantic events"

    assert script.cursor == 1
    assert len(conversations.runs) == 1
    assert all(run[1].startswith("react:") for run in conversations.runs)


async def test_decline_resolver_denies_approvals_and_answers_calls() -> None:
    requests = DeferredToolRequests(
        calls=[
            ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="c1")
        ],
        approvals=[
            ToolCallPart(tool_name="dangerous", args={}, tool_call_id="a1")
        ],
    )

    results = await DeclineResolver().resolve(requests)

    denied = results.approvals["a1"]
    assert isinstance(denied, ToolDenied) and "no user" in denied.message
    assert "no user" in cast(str, results.calls["c1"])


async def test_non_interactive_run_declines_deferrals_and_continues() -> None:
    """A non-interactive inkling run resolves every deferral as a decline
    in-process — the loop continues to a final answer instead of parking a
    DeferredToolRequests output."""
    agent, script = build_scripted_agent(
        [
            ScriptedTurn(
                tool_name="ask_questions",
                args={"questions": [{"question": "what's your name?"}]},
                tool_call_id="call_ask_1",
            ),
            "proceeding without answers",
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(agent, conversations)

    result = await tentacle.run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
        interactive=False,
    )

    assert result.output == "proceeding without answers"
    assert script.cursor == 2
    # The decline reached the model as the ask tool's return.
    recorded = str(conversations.store[(_THREAD, "inkling", "")].messages)
    assert "no user" in recorded


async def test_inkling_tentacle_invokes_suspender_on_deferred_request() -> None:
    """The real InklingTentacle -> react graph must invoke a supplied suspender
    when the run yields DeferredToolRequests (the persist+present contract)."""

    agent, _ = build_scripted_agent(
        [
            ScriptedTurn(
                tool_name="ask_questions",
                args={"questions": [{"question": "what's your name?"}]},
                tool_call_id="call_ask_1",
            ),
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(agent, conversations)
    suspender = StubSuspender()

    outputs: list[object] = []
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
        deferred_suspender=suspender,
    ) as stream:
        async for event in stream:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(event.result.output)

    assert len(suspender.suspended) == 1
    assert isinstance(suspender.suspended[0], DeferredToolRequests)
    assert suspender.suspended[0] is outputs[-1]


async def test_inkling_tentacle_stream_events_forwards_graph_events() -> None:
    agent, script = build_scripted_agent(
        [
            "all done!",
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(agent, conversations)

    captured_events: list[InklingTestEvent] = []
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    ) as stream:
        async for event in stream:
            captured_events.append(event)

    result_events = [
        event for event in captured_events if isinstance(event, AgentRunResultEvent)
    ]
    assert result_events
    assert result_events[-1].result.output == "all done!"
    assert script.cursor == 1


async def test_run_resumes_via_resume_turn_when_deferred_results_passed() -> None:
    """`run` with deferred_tool_results starts the graph from ResumeTurn: the
    resolved answers feed the recorded deferral and the loop continues."""

    agent, script = build_scripted_agent(
        [
            ScriptedTurn(
                tool_name="ask_questions",
                args={"questions": [{"question": "what's your name?"}]},
                tool_call_id="call_ask_1",
            ),
            "all done!",
        ]
    )
    conversations = FakeConversationManager()
    tentacle = _tentacle(agent, conversations)

    first = await tentacle.run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    )
    assert isinstance(first.output, DeferredToolRequests)

    results = DeferredToolResults()
    results.calls["call_ask_1"] = ["Ada"]
    resumed = await tentacle.run(
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
        deferred_tool_results=results,
    )

    assert resumed.output == "all done!"
    assert script.cursor == 2
    assert len(conversations.runs) == 2


async def test_subagent_run_mounts_no_tentacle_capabilities() -> None:
    """The tentacle's own capabilities (ask/send/todos/history) are user- or
    surface-coupled: `subagent_run` mounts none of them, while a plain run —
    interactive or not — keeps them. `interactive` only governs interaction."""
    seen_tools: list[list[str]] = []

    async def probe(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        seen_tools.append([tool.name for tool in info.function_tools])
        yield "ok"

    agent: Agent[None, ScriptedOutput] = Agent(
        FunctionModel(stream_function=probe, model_name="probe"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        system_prompt=SYSTEM_PROMPT,
    )
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=cast(Agent[None, InklingOutput], agent),
        conversation_manager=conversations,
        capabilities=[AskCapability(), TodoCapability()],
    )

    await tentacle.run(
        "hi",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    )
    parent = await conversations.ensure(_THREAD, agent_tentacle_id="inkling")
    child = await conversations.ensure(
        _THREAD,
        agent_tentacle_id="inkling",
        subagent_id="probe",
        parent_conversation_id=parent.id,
    )
    await tentacle.subagent_run(
        "work the brief",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        conversation_id=child.id,
    )
    await tentacle.subagent_run(
        "work with todos",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        conversation_id=child.id,
        capabilities=[TodoCapability()],
    )

    interactive_tools, accomplice_tools, chosen_tools = seen_tools
    assert "ask_questions" in interactive_tools
    assert "write_todos" in interactive_tools
    assert accomplice_tools == []
    # The spawner controls the set outright: what it passes is what mounts.
    assert "write_todos" in chosen_tools and "ask_questions" not in chosen_tools


async def test_inkling_default_includes_todo_capability() -> None:
    """The todo capability is on by default: its tools are offered to the model."""

    agent = _inkling_agent()
    seen_tools: list[str] = []

    async def respond_stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        seen_tools.extend(tool.name for tool in info.function_tools)
        yield "ok"

    await agent.run(
        "hi",
        output_type=STR_OUTPUT,
        model=FunctionModel(stream_function=respond_stream, model_name="probe"),
    )

    assert "write_todos" in seen_tools
    assert "read_todos" in seen_tools


async def test_inkling_default_output_is_segments() -> None:
    """The real inkling contract: with no output_type override the reply is a
    list of output segments (TestModel auto-generates from the segment schema)."""

    agent = _inkling_agent()
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
    )

    result = await tentacle.run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        model=TestModel(
            call_tools=[],
            custom_output_args=[
                {"type": "markdown", "data": {"text": "hello from the reef"}}
            ],
        ),
    )

    assert isinstance(result.output, list)
    assert all(isinstance(segment, Segment) for segment in result.output)
    assert [str(segment) for segment in result.output] == ["hello from the reef"]


async def test_inkling_loop_propagates_graph_error_streaming() -> None:
    """A model/graph error during a streamed run must surface to the caller
    rather than be swallowed by the background graph task (which would otherwise
    cancel the consumer mid-event and mask the real error)."""

    conversations = FakeConversationManager()
    tentacle = _tentacle(_boom_agent(), conversations)

    with pytest.raises(RuntimeError, match="model boom"):
        async with tentacle.run_stream_events(
            "hi octomate",
            conversation_address=_test_conversation_address(),
            thread_id=_THREAD,
        ) as stream:
            async for _ in stream:
                pass


async def test_inkling_loop_propagates_graph_error_collected_run() -> None:
    """`run` collects graph events internally; a graph error must still surface
    to the caller rather than be lost in the background task."""

    conversations = FakeConversationManager()
    tentacle = _tentacle(_boom_agent(), conversations)

    with pytest.raises(RuntimeError, match="model boom"):
        await tentacle.run(
            "hi octomate",
            conversation_address=_test_conversation_address(),
            thread_id=_THREAD,
            output_type=STR_OUTPUT,
        )
