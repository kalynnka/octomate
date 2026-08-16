"""InklingTentacle run entrypoints driving the real react graph against
scripted FunctionModels (no real LLM call)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TypeAlias, cast

import pytest
from pydantic_ai import (
    AgentRunResult,
    AgentRunResultEvent,
    RunContext,
    ToolDenied,
)
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import GraphRunContext
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.ask import ASK_DEFERR_KIND, AskCapability
from octomate.capabilities.gateway import TELEPORT_DEFER_KIND, GatewayCapability
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.deferred import DeclineResolver, PostureResolver
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.capabilities.harness.react import (
    ReactDeps,
    ReactState,
    ReactStreamEvent,
    ResolveDeferred,
    RunAgent,
)
from octomate.capabilities.todos import TodoCapability
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.segments import MessageSegment, Segment
from octomate.tentacles.agents.inkling import (
    InklingTentacle,
)
from octomate.tentacles.agents.inkling.base import InklingDeferrals, InklingOutput
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.types.permissions import AgentPermissionMode
from tests.support.agents import (
    ScriptedOutput,
    ScriptedTurn,
    build_scripted_agent,
    emit_scripted_turn,
)
from tests.support.managers import FakeConversation, FakeConversationManager

InklingTestEvent: TypeAlias = ReactStreamEvent[ScriptedOutput]


def _inkling_agent() -> Agent[None, InklingOutput]:
    return Agent(
        TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        capabilities=[
            AskCapability(),
            TodoCapability(),
            GatewayCapability(
                channel_routes={},
                current_agent_id="inkling",
            ),
        ],
        system_prompt=SYSTEM_PROMPT,
    )


@dataclass
class StubSuspender:
    suspended: list[DeferredToolRequests] = field(default_factory=list)

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        self.suspended.append(requests)
        return None


@dataclass
class UsageLimitProbe(AbstractCapability[None]):
    request_limit: int | None = None

    async def before_model_request(
        self,
        ctx: RunContext[None],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        if ctx.usage_limits is None:
            raise RuntimeError("agent run has no usage limits")
        self.request_limit = ctx.usage_limits.request_limit
        return request_context


_THREAD = uuid7()


def _test_conversation_address() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="test",
        chat_type="dm",
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
        calls=[ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="c1")],
        approvals=[ToolCallPart(tool_name="dangerous", args={}, tool_call_id="a1")],
    )

    results = await DeclineResolver().resolve(requests)

    denied = results.approvals["a1"]
    assert isinstance(denied, ToolDenied)
    assert "no user" in denied.message
    assert "no user" in cast(str, results.calls["c1"])


@pytest.mark.parametrize("approve", [True, False])
async def test_a_posture_resolver_answers_approvals_and_questions_only(
    approve: bool,
) -> None:
    """The two things a posture speaks about, and nothing else."""
    resolver = PostureResolver(approve=approve, message="you decide")
    questions = DeferredToolRequests(
        calls=[ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="c1")],
        approvals=[ToolCallPart(tool_name="dangerous", args={}, tool_call_id="a1")],
        metadata={"c1": {"kind": ASK_DEFERR_KIND}},
    )

    results = await resolver.resolve(questions)

    assert results.calls["c1"] == "you decide"
    if approve:
        assert results.approvals["a1"] is True
    else:
        denied = results.approvals["a1"]
        assert isinstance(denied, ToolDenied)
        assert denied.message == "you decide"

    # A teleport, and a deferral arriving with no declared kind at all, are neither an
    # approval nor a question. The resolver walks past both, which leaves the batch
    # short of an answer — and a short batch is the suspender's, not the loop's.
    for unowned, metadata in (
        ("teleport", {"kind": TELEPORT_DEFER_KIND, "hint": "over here"}),
        ("mystery", {}),
    ):
        mixed = DeferredToolRequests(
            calls=[
                ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="c1"),
                ToolCallPart(tool_name=unowned, args={}, tool_call_id="c2"),
            ],
            metadata={"c1": {"kind": ASK_DEFERR_KIND}, "c2": metadata},
        )

        partial = await resolver.resolve(mixed)

        assert set(partial.calls) == {"c1"}


async def test_a_bypassing_conversation_resolves_in_process_with_a_human_present() -> (
    None
):
    """An interactive run normally parks its deferrals for a card. Under
    `bypassPermissions` the conversation has said it wants no gate, so the question is
    answered in-process — the agent is told to decide — and the run continues to a
    final answer instead."""
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
    conversations.store[(_THREAD, "inkling", "")] = FakeConversation(
        thread_id=_THREAD,
        agent_tentacle_id="inkling",
        permission_mode="bypassPermissions",
    )
    tentacle = _tentacle(agent, conversations)

    result = await tentacle.run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    )

    assert result.output == "proceeding without answers"
    assert script.cursor == 2
    recorded = str(conversations.store[(_THREAD, "inkling", "")].messages)
    assert "approvals go through without one" in recorded


async def test_a_dont_ask_conversation_answers_its_own_questions() -> None:
    """`dontAsk` is about the question, not the gate: a human is there, and this
    conversation said not to interrupt them, so the ask resolves in-process and the
    agent proceeds on its own judgment."""
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
    conversations.store[(_THREAD, "inkling", "")] = FakeConversation(
        thread_id=_THREAD, agent_tentacle_id="inkling", permission_mode="dontAsk"
    )
    tentacle = _tentacle(agent, conversations)

    result = await tentacle.run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    )

    assert result.output == "proceeding without answers"
    assert script.cursor == 2
    # The reason names the posture rather than a missing human, since there is one.
    recorded = str(conversations.store[(_THREAD, "inkling", "")].messages)
    assert "do not want questions or approval prompts" in recorded


async def test_a_teleport_reaches_the_suspender_under_a_bypassing_posture() -> None:
    """A posture speaks about approvals and questions. A `teleport` is deferred the
    same way but is neither — only the suspender performs one — so no posture may
    answer it out from under the human. It defers again and lands there."""
    agent, _ = build_scripted_agent(
        [
            ScriptedTurn(
                tool_name="teleport",
                args={"hint": "carrying on over here"},
                tool_call_id="call_tp_1",
            ),
        ]
    )
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "inkling", "")] = FakeConversation(
        thread_id=_THREAD,
        agent_tentacle_id="inkling",
        permission_mode="bypassPermissions",
    )
    suspender = StubSuspender()

    await _tentacle(agent, conversations).run(
        "take this elsewhere",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
        deferred_suspender=suspender,
        capabilities=[GatewayCapability(channel_routes={}, current_agent_id="inkling")],
    )

    [suspended] = suspender.suspended
    assert [call.tool_name for call in suspended.calls] == ["teleport"]


async def test_a_question_batched_with_a_teleport_suspends_whole() -> None:
    """A batch is answered together or not at all — the runner refuses results that
    cover only some of its deferred calls. So a posture that has no opinion about the
    teleport has none about the question beside it either, and the human gets both.
    The teleport surviving is what matters; taking the question along is the price of
    a batch being indivisible, and it is what `default` does with the pair anyway."""

    async def both(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        yield {
            0: DeltaToolCall(
                name="ask_questions",
                json_args=json.dumps({"questions": [{"question": "which one?"}]}),
                tool_call_id="call_ask_1",
            ),
            1: DeltaToolCall(
                name="teleport",
                json_args=json.dumps({"hint": "carrying on over here"}),
                tool_call_id="call_tp_1",
            ),
        }

    agent: Agent[None, ScriptedOutput] = Agent(
        FunctionModel(stream_function=both, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        capabilities=[
            AskCapability(),
            GatewayCapability(channel_routes={}, current_agent_id="inkling"),
        ],
        system_prompt=SYSTEM_PROMPT,
    )
    conversations = FakeConversationManager()
    conversation = FakeConversation(
        thread_id=_THREAD,
        agent_tentacle_id="inkling",
        permission_mode="bypassPermissions",
    )
    conversations.store[(_THREAD, "inkling", "")] = conversation
    suspender = StubSuspender()

    await _tentacle(agent, conversations).run(
        "ask me and then move us",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
        deferred_suspender=suspender,
    )

    assert "approvals go through without one" not in str(conversation.messages)
    [suspended] = suspender.suspended
    assert [call.tool_name for call in suspended.calls] == [
        "ask_questions",
        "teleport",
    ]


@pytest.mark.parametrize(
    ("interactive", "permission_mode", "expected"),
    [
        # A human is there, so only what the posture speaks about is answered and
        # the rest of the batch is theirs.
        (True, "dontAsk", [PostureResolver]),
        (True, "bypassPermissions", [PostureResolver]),
        # `default` and an undeclared posture both fall to the human whole.
        (True, "default", []),
        (True, None, []),
        # Nobody to ask and no suspender, so a catch-all closes the chain and every
        # batch comes out covered. The posture still speaks first when there is one.
        (False, None, [DeclineResolver]),
        (False, "bypassPermissions", [PostureResolver, DeclineResolver]),
    ],
)
def test_which_deferrals_inkling_answers_itself(
    interactive: bool,
    permission_mode: str | None,
    expected: list[type[object]],
) -> None:
    conversation = FakeConversation(
        thread_id=_THREAD,
        agent_tentacle_id="inkling",
        permission_mode=cast("AgentPermissionMode | None", permission_mode),
    )
    deferrals = InklingDeferrals(
        interactive=interactive, configured="default", fallback=None
    )

    chain = deferrals(cast("Conversation", conversation))

    assert [type(resolver) for resolver in chain] == expected


async def test_the_posture_speaks_first_and_the_catch_all_takes_what_is_left() -> None:
    """The chain's whole point, on the one batch that needs both: a bypassing
    accomplice grants its approval through the posture, and the teleport nobody has
    an opinion about is closed out by the catch-all rather than parked for a human
    who is not there."""
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "inkling", "")] = FakeConversation(
        thread_id=_THREAD,
        agent_tentacle_id="inkling",
        permission_mode="bypassPermissions",
    )
    requests = DeferredToolRequests(
        calls=[
            ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="c1"),
            ToolCallPart(tool_name="teleport", args={}, tool_call_id="c2"),
        ],
        approvals=[ToolCallPart(tool_name="dangerous", args={}, tool_call_id="a1")],
        metadata={"c1": {"kind": ASK_DEFERR_KIND}, "c2": {"kind": TELEPORT_DEFER_KIND}},
    )
    node: ResolveDeferred[ScriptedOutput, None] = ResolveDeferred(
        requests=requests, result=AgentRunResult(requests)
    )
    ctx: GraphRunContext[ReactState, ReactDeps[ScriptedOutput, None]] = GraphRunContext(
        state=ReactState(
            conversation_address=_test_conversation_address(),
            agent_tentacle_id="inkling",
            thread_id=_THREAD,
        ),
        deps=ReactDeps(
            agent=cast("Agent[None, ScriptedOutput]", object()),
            conversation_manager=conversations,
            agent_deps=None,
            choose_resolvers=InklingDeferrals(
                interactive=False, configured="default", fallback=None
            ),
        ),
    )

    nxt = await node.run(ctx)

    # Covered between them, so the loop carries on rather than parking anything.
    assert isinstance(nxt, RunAgent)
    results = nxt.deferred_results
    assert results is not None
    # The posture answered first and its answers stand — the catch-all only filled
    # the gap it left.
    assert results.approvals["a1"] is True
    assert "approvals go through without one" in cast(str, results.calls["c1"])
    assert "no user to ask" in cast(str, results.calls["c2"])


async def test_a_posture_switched_mid_run_lands_on_the_next_round() -> None:
    """The whole point of reading the posture at each deferral: a switch made while
    the run is going binds the round after it, not the run after it.

    Both postures decline the ask, so what tells them apart is the reason the agent
    is given — and both reasons are in one run's history here."""
    turns: list[ScriptedTurn | str] = [
        ScriptedTurn(
            tool_name="ask_questions",
            args={"questions": [{"question": "first?"}]},
            tool_call_id="call_ask_1",
        ),
        ScriptedTurn(
            tool_name="ask_questions",
            args={"questions": [{"question": "second?"}]},
            tool_call_id="call_ask_2",
        ),
        "proceeding without answers",
    ]
    conversations = FakeConversationManager()
    conversation = FakeConversation(
        thread_id=_THREAD,
        agent_tentacle_id="inkling",
        permission_mode="bypassPermissions",
    )
    conversations.store[(_THREAD, "inkling", "")] = conversation

    turn = 0

    def switching(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal turn
        # The console's ⇧⇥ landing between the first deferral and the second.
        if turn == 1:
            conversation.permission_mode = "dontAsk"
        current = turns[turn]
        turn += 1
        return emit_scripted_turn(current)

    agent: Agent[None, ScriptedOutput] = Agent(
        FunctionModel(stream_function=switching, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        capabilities=[AskCapability()],
        system_prompt=SYSTEM_PROMPT,
    )

    result = await _tentacle(agent, conversations).run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
    )

    assert result.output == "proceeding without answers"
    recorded = str(conversation.messages)
    assert "approvals go through without one" in recorded
    assert "do not want questions or approval prompts" in recorded


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


async def test_subagent_run_mounts_the_tentacle_capabilities_too() -> None:
    """An accomplice gets the tentacle's own capabilities, same as a plain run,
    plus whatever its spawner adds. `interactive=False` governs interaction, not
    what is mounted: the human-facing tools are still offered, they just decline
    in-process instead of parking a batch nobody is there to answer."""
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
        capabilities=[AskCapability()],
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
    # The accomplice is served the same set, without having asked for any of it.
    assert accomplice_tools == interactive_tools
    # What the spawner passes is added to that set, not substituted for it — so a
    # spawner must not pass a capability the tentacle already holds, since two
    # toolsets offering one name is a hard error.
    assert "write_todos" in chosen_tools
    assert "ask_questions" in chosen_tools


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


async def test_inkling_applies_its_configured_request_limit() -> None:
    agent = _inkling_agent()
    conversations = FakeConversationManager()
    probe = UsageLimitProbe()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
        capabilities=[probe],
        request_limit=17,
    )

    await tentacle.run(
        "hi octomate",
        conversation_address=_test_conversation_address(),
        thread_id=_THREAD,
        output_type=STR_OUTPUT,
        model=TestModel(call_tools=[]),
    )

    assert probe.request_limit == 17


async def test_inkling_loop_propagates_graph_error_streaming() -> None:
    """A model/graph error during a streamed run must surface to the caller
    rather than be swallowed by the background graph task (which would otherwise
    cancel the consumer mid-event and mask the real error)."""

    conversations = FakeConversationManager()
    tentacle = _tentacle(_boom_agent(), conversations)

    # The block cannot shrink to one statement: what is asserted is that the error
    # escapes the streaming loop rather than the call that opened it.
    with pytest.raises(RuntimeError, match="model boom"):  # noqa: PT012
        async with tentacle.run_stream_events(
            "hi octomate",
            conversation_address=_test_conversation_address(),
            thread_id=_THREAD,
        ) as stream:
            async for _ in stream:
                pass

    assert len(conversations.runs) == 1
    assert "hi octomate" in str(conversations.runs[0][2])


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

    assert len(conversations.runs) == 1
    assert "hi octomate" in str(conversations.runs[0][2])
