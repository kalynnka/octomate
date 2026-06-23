"""The triage graph: Awake, Route, RunTriage, PrepareReception, RunReception,
and ResumeDeferred — driven with the canonical fake agent/channel/managers."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.capabilities.summon import SCRY_TOOL_NAME, SummonCapability
from octomate.config import AgentModelConfig, ChannelConfig, ChannelStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse, UserMessageSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.triage import (
    DeferredResult,
    DirectAnswerDecision,
    SummonDecision,
    ResponseTarget,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.triage.graph import (
    Awake,
    ResumeDeferred,
    Route,
    RunReception,
    RunTriage,
)
from octomate.tentacles.channel.feelers.output import TimelineState
from tests.support.agents import FakeAgent, RecordedRun
from tests.support.channels import FakeChannelTentacle, RecordingMarkdownFeeler
from tests.support.managers import (
    FakeActionManager,
    FakeConversationManager,
    FakeDeferredBatch,
    FakePresentedBatch,
)

FAKE_CONTEXT = cast(RunContext[None], None)


def _channel(*, stream: bool = True) -> FakeChannelTentacle:
    return FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=stream),
            receptions=[AgentModelConfig(agent="other")],
        )
    )


class DroppingTimelineState(TimelineState):
    async def drive(
        self,
        stream: AsyncIterator[object],
    ) -> None:
        return  # abandon the reception stream without draining it


class DroppingTimelineFeeler:
    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncGenerator[DroppingTimelineState]:
        yield DroppingTimelineState()


class DroppingChannel(FakeChannelTentacle):
    """A channel whose timeline abandons the reception stream without producing a
    result (exercises the fail-fast guard)."""

    def __init__(self, *, config: ChannelConfig) -> None:
        super().__init__(config=config)
        self.feelers.timeline = DroppingTimelineFeeler()


def _key(thread_id: str = "") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id=thread_id,
    )


def _source_target(address: ChannelAddress) -> ResponseTarget:
    return ResponseTarget(
        channel_id="im",
        address=address,
        thread_strategy="flat_thread",
        mode="main",
    )


def _state(
    address: ChannelAddress,
    *,
    user_prompt: str | None = "hi",
) -> TriageState:
    return TriageState(
        source_target=_source_target(address),
        user_prompt=user_prompt,
    )


def _deps(
    *,
    conversations: FakeConversationManager,
    channels: dict[str, FakeChannelTentacle],
    agent: FakeAgent,
    action_manager: FakeActionManager | None = None,
) -> TriageDeps:
    return TriageDeps(
        channels=dict(channels),
        agents={"inkling": agent, "other": agent, agent.id: agent},
        conversation_manager=conversations,
        action_manager=cast(
            DeferredActionManager, action_manager or FakeActionManager()
        ),
    )


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "What should I clarify?"}]},
                tool_call_id="call_question",
            )
        ]
    )


def _deferred_results() -> DeferredToolResults:
    results = DeferredToolResults()
    results.calls["call_question"] = ["please answer directly"]
    return results


def _recorded_summon_capability(run: RecordedRun) -> SummonCapability:
    summons = [
        capability
        for capability in run.capabilities
        if isinstance(capability, SummonCapability)
    ]
    assert len(summons) == 1
    return summons[0]


def test_available_routes_skip_disconnected_reception_agents() -> None:
    channel = FakeChannelTentacle(
        id="chan1",
        config=ChannelConfig(
            type="fake",
            receptions=[
                AgentModelConfig(agent="claude", model="opus"),
                AgentModelConfig(agent="other"),
            ],
        ),
    )
    other = FakeAgent(id="other")
    deps = TriageDeps(
        channels={"chan1": channel},
        agents={"other": other},
        conversation_manager=FakeConversationManager(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )

    routes = deps.available_routes["chan1"]

    assert [(route.agent_id, route.model) for route in routes] == [("other", "")]
    assert deps.available_routes["chan1"] is routes


async def test_triage_mounts_summon_capability_without_routes() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="hello",
            reason="answered",
        )
    )
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[],
        )
    )

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    summon = _recorded_summon_capability(agent.turns[0])
    assert not isinstance(result, DeferredResult)
    assert summon.toolset is not None
    scry = summon.toolset.tools[SCRY_TOOL_NAME].function
    assert await scry(FAKE_CONTEXT) == []


async def test_reception_mounts_summon_capability_without_routes() -> None:
    address = _key()
    decision = SummonDecision(
        action="summon",
        agent_id="other",
        model="",
        reason="needs work",
        hint="Working on it",
        summon="Please debug this in reception.",
    )
    agent = FakeAgent(
        id="other",
        allow_reception_run=True,
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            receptions=[AgentModelConfig(agent="other")],
        )
    )
    target = _source_target(address)

    result = (
        await triage_graph.run(
            RunReception(),
            state=TriageState(
                source_target=target,
                target=target,
                decision=decision,
            ),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    summon = _recorded_summon_capability(agent.turns[0])
    assert not isinstance(result, DeferredResult)
    assert summon.toolset is not None
    scry = summon.toolset.tools[SCRY_TOOL_NAME].function
    assert await scry(FAKE_CONTEXT) == []


async def test_triage_graph_emits_direct_route() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="hello",
            reason="answered",
        )
    )
    conversations = FakeConversationManager()
    im = _channel()
    ops = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address),
            deps=_deps(
                conversations=conversations,
                channels={"im": im, "ops": ops},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision.answer == "hello"
    assert result.target.channel_id == "im"
    assert result.result is not None and result.result.output.answer == "hello"
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert agent.streams == []
    assert conversations.ensured == []
    assert im.sent[0][2][0]["text"] == "hello"
    assert im.consumed == []
    assert ops.sent == []


async def test_triage_graph_returns_deferred_result_when_triage_requests_input() -> (
    None
):
    address = _key()
    requests = _requests()
    agent = FakeAgent(triage_output=requests)
    conversations = FakeConversationManager()
    action_manager = FakeActionManager()
    im = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert isinstance(result, DeferredResult)
    assert result.requests is requests
    assert result.result.output is requests
    assert result.run_name == "triage"
    assert result.target == _source_target(address)
    assert len(action_manager.create_calls) == 1
    create_call = action_manager.create_calls[0]
    assert create_call.run_name == "triage"
    assert create_call.source_address == address
    assert create_call.target_address == address
    assert create_call.target_mode == "main"
    assert create_call.decision is None
    assert create_call.requests is requests
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert [turn.prompt for turn in agent.turns] == ["hi"]
    # The suspender ensures the conversation when persisting the batch.
    assert conversations.ensured == [(address, "inkling")]
    assert im.sub_threads == []


async def test_triage_graph_resumes_completed_triage_deferred_batch() -> None:
    address = _key()
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_address=address,
        target_address=address,
        requests=_requests(),
        deferred_results=deferred_results,
    )
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="answered",
            hint="continuing",
            summon="continue after the answered question",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = (
        await triage_graph.run(
            ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert isinstance(result.decision, SummonDecision)
    assert result.target.channel_id == "im"
    assert result.target.mode == "sub"
    assert result.result is not None and result.result.output == "done"
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert [turn.prompt for turn in agent.turns] == [None]
    assert [turn.deferred_results for turn in agent.turns] == [deferred_results]
    assert agent.streams[0].run_name == "reception"
    assert agent.streams[0].deferred_results is None
    # RunTriage no longer threads history through the graph — react loads it
    # from the conversation (which carries the deferred turn the create run
    # recorded), so the agent is called with no explicit history.
    assert [turn.history for turn in agent.turns] == [[]]
    # ResumeDeferred no longer touches the conversation; history is loaded by
    # react when it runs, which the fake agent here bypasses.
    assert conversations.ensured == []
    assert action_manager.marked == [
        (batch.id, "resuming", False),
        (batch.id, "completed", True),
    ]
    assert im.sent[-1][2][0]["text"] == "done"


async def test_triage_graph_keeps_incomplete_triage_batch_deferred() -> None:
    address = _key()
    batch = FakeDeferredBatch(
        source_address=address,
        target_address=address,
        requests=_requests(),
        deferred_results=_deferred_results(),
        status="pending",
        completed=False,
    )
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="hello",
            reason="answered",
        )
    )
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = (
        await triage_graph.run(
            ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert isinstance(result, DeferredResult)
    assert result.requests is batch.requests
    assert result.result.output is batch.requests
    assert result.run_name == "triage"
    assert agent.turns == []
    assert conversations.ensured == []
    assert action_manager.marked == []
    assert im.sent == []


async def test_triage_graph_uses_markdown_feeler_for_direct_answer() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="hello",
            reason="answered",
        )
    )
    conversations = FakeConversationManager()
    im = _channel()
    markdown_feeler = RecordingMarkdownFeeler()
    im.feelers.markdown = markdown_feeler

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision.answer == "hello"
    assert markdown_feeler.calls == [(address, "hello")]
    assert im.sent == []


async def test_per_channel_routing_resolves_agent_and_model() -> None:
    triage_fake = FakeAgent(
        id="t2",
        triage_output=SummonDecision(
            action="summon",
            agent_id="r2",
            model="opus",
            reason="needs work",
            hint="needs work",
            summon="Please debug this in reception.",
        ),
    )
    reception_fake = FakeAgent(
        id="r2", reception_output="done", models={"opus": "opus-model"}
    )
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            triage=AgentModelConfig(agent="t2"),
            receptions=[AgentModelConfig(agent="r2", model="opus")],
        )
    )
    ops = _channel()
    deps = TriageDeps(
        channels={"im": im, "ops": ops},
        agents={"t2": triage_fake, "r2": reception_fake},
        conversation_manager=conversations,
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )

    result = (
        await triage_graph.run(
            RunTriage(), state=_state(_key(), user_prompt="debug this"), deps=deps
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.result is not None and result.result.output == "done"
    assert [turn.run_name for turn in triage_fake.turns] == ["triage"]
    assert triage_fake.streams == []
    assert [stream.run_name for stream in reception_fake.streams] == ["reception"]
    assert reception_fake.streams[0].model == "opus-model"
    assert reception_fake.turns == []


async def test_triage_graph_emits_reception_after_route() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="needs work",
            hint="Working on it",
            summon="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()
    ops = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im, "ops": ops},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.target.channel_id == "im"
    assert result.target.mode == "sub"
    assert result.result is not None
    assert result.result.output == "done"
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert agent.streams[0].address.thread_id == "hint-thread"
    assert agent.streams[0].run_name == "reception"
    assert agent.streams[0].prompt == "Please debug this in reception."
    # The triage graph no longer ensures conversations for runs; react loads
    # history from the (sub-thread) conversation itself.
    assert conversations.ensured == []
    assert im.sub_threads[0][1] == "Working on it"
    assert im.consumed[0][0].thread_id == "hint-thread"
    assert im.sent[-1][2][0]["text"] == "done"
    assert ops.sent == []


async def test_triage_resume_does_not_leak_deferred_results_into_reception() -> None:
    # A triage-phase question is answered, and the triage run then decides to
    # open a reception. The triage answer must NOT leak into that fresh
    # sub-thread run — it would resume an empty conversation and raise
    # "Tool call results were provided, but the message history is empty."
    address = _key()
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_address=address,
        target_address=address,
        requests=_requests(),
        deferred_results=deferred_results,
    )
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="needs work",
            hint="Working on it",
            summon="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()
    ops = _channel()

    await triage_graph.run(
        ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
        state=TriageState(),
        deps=_deps(
            conversations=conversations,
            channels={"im": im, "ops": ops},
            agent=agent,
            action_manager=action_manager,
        ),
    )

    # The triage run consumed the deferred answer ...
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert [turn.deferred_results for turn in agent.turns] == [deferred_results]
    # ... and the following reception ran fresh, WITHOUT the leaked answer.
    assert agent.streams[0].run_name == "reception"
    assert [stream.deferred_results for stream in agent.streams] == [None]


async def test_triage_graph_consumes_reception_stream() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="needs work",
            hint="Working on it",
            summon="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()
    ops = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im, "ops": ops},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.result is not None
    assert result.result.output == "done"
    # Reception streamed through the source channel's consumer, not markdown.
    assert len(im.consumed) == 1
    assert im.consumed[0][0].thread_id == "hint-thread"
    assert im.sent[-1][2][0]["text"] == "done"
    assert ops.consumed == []
    assert ops.sent == []


async def test_triage_graph_fails_fast_when_stream_produces_no_result() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="needs work",
            hint="Working on it",
            summon="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = DroppingChannel(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[AgentModelConfig(agent="other")],
        )
    )
    ops = _channel()

    with pytest.raises(RuntimeError, match="completed without a result"):
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im, "ops": ops},
                agent=agent,
            ),
        )

    assert len(agent.streams) == 0


async def test_triage_graph_runs_final_reception_without_stream_when_disabled() -> None:
    address = _key()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="needs work",
            hint="needs work",
            summon="Please finish this without streaming.",
        ),
        reception_output="done",
        allow_reception_run=True,
    )
    conversations = FakeConversationManager()
    im = _channel(stream=False)

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.result is not None
    assert result.result.output == "done"
    assert [turn.run_name for turn in agent.turns] == ["triage", "reception"]
    assert agent.streams == []
    assert im.sub_threads[0][1] == "needs work"
    assert im.sent[0][3] == "hint-thread"  # reply target is the sub-thread
    assert im.sent[0][2][0]["text"] == "done"


async def test_triage_graph_returns_deferred_result_when_streaming_reception_requests_input() -> (
    None
):
    address = _key()
    requests = _requests()
    decision = SummonDecision(
        action="summon",
        agent_id="other",
        model="",
        reason="needs work",
        hint="needs work",
        summon="Please ask before continuing.",
    )
    agent = FakeAgent(
        triage_output=decision,
        reception_output=requests,
    )
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(
        presented_batch=FakePresentedBatch(
            questions=[
                DeferredQuestion(
                    tool_name="ask_questions",
                    tool_call_id="call_question",
                    position=0,
                    args={"question": "What should I clarify?"},
                    metadata={},
                )
            ]
        )
    )
    im = _channel(stream=True)

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert isinstance(result, DeferredResult)
    assert result.run_name == "reception"
    assert result.requests is requests
    assert result.result.output is requests
    assert result.batch_id == action_manager.create_calls[0].batch_id
    create_call = action_manager.create_calls[0]
    assert create_call.run_name == "reception"
    assert create_call.decision == decision
    assert create_call.source_address == address
    assert create_call.target_address.thread_id == "hint-thread"
    assert agent.streams[0].run_name == "reception"


async def test_reception_can_summon_another_reception_agent() -> None:
    address = _key()
    triage_agent = FakeAgent(
        id="triage",
        triage_output=SummonDecision(
            action="summon",
            agent_id="first",
            model="",
            reason="needs first pass",
            hint="First pass",
            summon="First agent brief.",
        ),
    )
    first_agent = FakeAgent(
        id="first",
        reception_summon=SummonDecision(
            action="summon",
            agent_id="second",
            model="",
            reason="needs second pass",
            hint="Second pass",
            summon="Second agent brief.",
        ),
        allow_reception_run=True,
    )
    second_agent = FakeAgent(
        id="second",
        reception_output="done",
        allow_reception_run=True,
    )
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            triage=AgentModelConfig(agent="triage"),
            receptions=[
                AgentModelConfig(agent="first"),
                AgentModelConfig(agent="second"),
            ],
        )
    )
    conversations = FakeConversationManager()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=TriageDeps(
                channels={"im": im},
                agents={
                    "triage": triage_agent,
                    "first": first_agent,
                    "second": second_agent,
                },
                conversation_manager=conversations,
                action_manager=cast(DeferredActionManager, FakeActionManager()),
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert isinstance(result.decision, SummonDecision)
    assert result.decision.agent_id == "second"
    assert result.result is not None and result.result.output == "done"
    assert [turn.run_name for turn in triage_agent.turns] == ["triage"]
    assert [turn.prompt for turn in first_agent.turns] == ["First agent brief."]
    assert [turn.prompt for turn in second_agent.turns] == ["Second agent brief."]
    assert len(im.sub_threads) == 1
    assert im.sub_threads[0][1] == "First pass"
    assert im.sent == [
        ("alice", "private", [{"text": "done"}], "hint-thread", False)
    ]


async def test_triage_graph_skips_triage_inside_flat_thread() -> None:
    address = _key(thread_id="existing-thread")
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="unused",
            reason="unused",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()

    result = (
        await triage_graph.run(
            Route(),
            state=_state(address, user_prompt="continue"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.target.channel_id == "im"
    assert result.target.mode == "sub"
    assert result.result is not None
    assert result.result.output == "done"
    assert agent.turns == []
    assert agent.streams[0].address == address
    assert agent.streams[0].run_name == "reception"
    assert agent.streams[0].prompt == "continue"
    assert im.sub_threads == []
    assert im.consumed[0][0] == address
    assert im.sent[-1][2][0]["text"] == "done"


async def test_flat_thread_skips_disconnected_reception_agents() -> None:
    address = _key(thread_id="existing-thread")
    agent = FakeAgent(reception_output="done")
    conversations = FakeConversationManager()
    im = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[
                AgentModelConfig(agent="claude", model="opus"),
                AgentModelConfig(agent="other"),
            ],
        )
    )

    result = (
        await triage_graph.run(
            Route(),
            state=_state(address, user_prompt="continue"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.result is not None
    assert result.result.output == "done"
    assert result.decision.agent_id == "other"
    assert agent.streams[0].run_name == "reception"


async def test_awake_short_circuits_on_empty_signal() -> None:
    agent = FakeAgent()
    conversations = FakeConversationManager()
    im = _channel()

    result = (
        await triage_graph.run(
            Awake(signal=UserMessageSignal([])),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision.reason == "Empty awake signal."
    assert agent.turns == []
    assert conversations.ensured == []
    assert im.sent == []


async def test_awake_short_circuits_on_empty_prompt() -> None:
    class BlankEvent(MessageEvent):
        """Renders to whitespace — `str(MessageEvent)` always carries a header,
        so the empty-prompt guard is only reachable through a blank rendering."""

        def __str__(self) -> str:
            return "   "

    agent = FakeAgent()
    conversations = FakeConversationManager()
    im = _channel()
    event = BlankEvent(
        tentacle_id="im",
        message_id="m1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        segments=[],
    )

    result = (
        await triage_graph.run(
            Awake(signal=UserMessageSignal([event])),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision.reason == "Empty user prompt."
    assert result.target.channel_id == "im"
    # The empty-prompt guard short-circuits in Awake, before Route ensures the
    # conversation, so nothing is ensured.
    assert conversations.ensured == []
    assert agent.turns == []
    assert im.sent == []


async def test_awake_raises_for_unknown_channel_or_agent() -> None:
    agent = FakeAgent()
    conversations = FakeConversationManager()
    im = _channel()
    event = MessageEvent(
        tentacle_id="ghost",
        message_id="m1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        segments=[TextSegment(data={"text": "hi"})],
    )

    with pytest.raises(ValueError, match="unknown channel"):
        await triage_graph.run(
            Awake(signal=UserMessageSignal([event])),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )

    stranger = _channel()  # its config names agent "inkling", not registered below
    deps = TriageDeps(
        channels={"im": stranger},
        agents={},
        conversation_manager=conversations,
        action_manager=cast(DeferredActionManager, FakeActionManager()),
    )
    event = MessageEvent(
        tentacle_id="im",
        message_id="m2",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        segments=[TextSegment(data={"text": "hi"})],
    )

    with pytest.raises(ValueError, match="unknown agent"):
        await triage_graph.run(
            Awake(signal=UserMessageSignal([event])),
            state=TriageState(),
            deps=deps,
        )


async def test_prepare_reception_falls_back_to_main_on_sub_thread_failure() -> None:
    class FailingSubThreadChannel(FakeChannelTentacle):
        async def start_sub_thread(
            self,
            address: ChannelAddress,
            hint_text: str,
        ) -> ChannelAddress:
            raise RuntimeError("platform refused the thread")

    address = _key()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
            reason="needs work",
            hint="needs work",
            summon="Please continue in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = FailingSubThreadChannel(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[AgentModelConfig(agent="other")],
        )
    )

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(address, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.target.mode == "main"
    # Reception ran at the main conversation address, not a sub-thread.
    assert im.consumed[0][0] == address
    assert agent.streams[0].address == address


async def test_resume_routes_reception_batch_to_run_reception() -> None:
    address = _key(thread_id="hint-thread")
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=deferred_results,
        run_name="reception",
        target_mode="sub",
    )
    agent = FakeAgent(reception_output="resumed answer")
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = (
        await triage_graph.run(
            ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.result is not None
    assert result.result.output == "resumed answer"
    # The resumed reception runs with the batch results and no fresh prompt.
    assert agent.turns == []
    assert agent.streams[0].prompt is None
    assert agent.streams[0].run_name == "reception"
    assert agent.streams[0].address == address
    assert [stream.deferred_results for stream in agent.streams] == [deferred_results]
    assert im.consumed[0][0] == address
    # The resumed batch is marked resuming, then completed after the run.
    assert action_manager.marked == [
        (batch.id, "resuming", False),
        (batch.id, "completed", True),
    ]


async def test_resume_returns_triage_result_for_already_completed_batch() -> None:
    address = _key(thread_id="hint-thread")
    decision = SummonDecision(
        action="summon",
        agent_id="inkling",
        model="",
        reason="resumed",
        hint="resumed",
        summon="",
    )
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=_deferred_results(),
        run_name="reception",
        target_mode="sub",
        decision=decision,
        status="completed",
    )
    agent = FakeAgent()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = (
        await triage_graph.run(
            ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision == decision
    assert agent.turns == []
    assert agent.streams == []
    assert action_manager.marked == []
    assert im.sent == []


async def test_resume_keeps_incomplete_reception_batch_deferred() -> None:
    address = _key(thread_id="hint-thread")
    batch = FakeDeferredBatch(
        source_address=_key(),
        target_address=address,
        requests=_requests(),
        deferred_results=_deferred_results(),
        run_name="reception",
        target_mode="sub",
        status="pending",
        completed=False,
    )
    agent = FakeAgent()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = _channel()

    result = (
        await triage_graph.run(
            ResumeDeferred(awake=DeferredActionBatchResponse(batch_id=batch.id)),
            state=TriageState(),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
                action_manager=action_manager,
            ),
        )
    ).output

    assert isinstance(result, DeferredResult)
    assert result.run_name == "reception"
    assert result.requests is batch.requests
    assert agent.streams == []
    assert action_manager.marked == []
