"""The triage graph: Awake, Route, RunTriage, PrepareReception, RunReception,
and ResumeDeferred — driven with the canonical fake agent/channel/managers."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse, UserMessageSignal
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.inkling.graph import (
    DeferredResult,
    ResponseTarget,
    TriageDecision,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.tentacles.agent.inkling.graph.triage import (
    Awake,
    ResumeDeferred,
    Route,
    RunTriage,
)
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.tentacles.channel.feelers.output import TimelineState
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle, RecordingMarkdownFeeler
from tests.support.managers import (
    FakeActionManager,
    FakeConversationManager,
    FakeDeferredBatch,
)


def _channel(*, stream: bool = True) -> FakeChannelTentacle:
    return FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            agent_id="inkling",
            stream=ChannelStreamConfig(enabled=stream),
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
    async def open(self, key: ConversationKey) -> AsyncGenerator[DroppingTimelineState]:
        yield DroppingTimelineState()


class DroppingChannel(FakeChannelTentacle):
    """A channel whose timeline abandons the reception stream without producing a
    result (exercises the fail-fast guard)."""

    def __init__(self, *, config: ChannelConfig) -> None:
        super().__init__(config=config)
        self.feelers.timeline = DroppingTimelineFeeler()


def _key(thread_id: str = "") -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id=thread_id,
    )


def _source_target(key: ConversationKey) -> ResponseTarget:
    return ResponseTarget(
        channel_id="im",
        key=key,
        thread_strategy="flat_thread",
        mode="main",
    )


def _state(
    key: ConversationKey,
    *,
    user_prompt: str | None = "hi",
) -> TriageState:
    return TriageState(
        source_target=_source_target(key),
        agent_id="inkling",
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
        agents={agent.id: cast(AgentTentacle[InklingOutput, None], agent)},
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


async def test_triage_graph_emits_direct_route() -> None:
    key = _key()
    agent = FakeAgent(triage_output=TriageDecision(action="answer", answer="hello"))
    conversations = FakeConversationManager()
    im = _channel()
    ops = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key),
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
    assert result.result is None
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert agent.streams == []
    assert conversations.ensured == []
    assert im.sent[0][2][0]["text"] == "hello"
    assert im.consumed == []
    assert ops.sent == []


async def test_triage_graph_returns_deferred_result_when_triage_requests_input() -> (
    None
):
    key = _key()
    requests = _requests()
    agent = FakeAgent(triage_output=requests)
    conversations = FakeConversationManager()
    action_manager = FakeActionManager()
    im = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key),
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
    assert result.target == _source_target(key)
    assert len(action_manager.create_calls) == 1
    create_call = action_manager.create_calls[0]
    assert create_call.run_name == "triage"
    assert create_call.source_key == key
    assert create_call.target_key == key
    assert create_call.target_mode == "main"
    assert create_call.decision is None
    assert create_call.requests is requests
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert [turn.prompt for turn in agent.turns] == ["hi"]
    # The suspender ensures the conversation when persisting the batch.
    assert conversations.ensured == [(key, "inkling")]
    assert im.sub_threads == []


async def test_triage_graph_resumes_completed_triage_deferred_batch() -> None:
    key = _key()
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_key=key,
        target_key=key,
        requests=_requests(),
        deferred_results=deferred_results,
    )
    agent = FakeAgent(triage_output=TriageDecision(action="answer", answer="hello"))
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
    assert result.decision.answer == "hello"
    assert result.target == _source_target(key)
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert [turn.prompt for turn in agent.turns] == [None]
    assert [turn.deferred_results for turn in agent.turns] == [deferred_results]
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
    assert len(im.sent) == 1
    assert im.sent[0][2][0]["text"] == "hello"


async def test_triage_graph_keeps_incomplete_triage_batch_deferred() -> None:
    key = _key()
    batch = FakeDeferredBatch(
        source_key=key,
        target_key=key,
        requests=_requests(),
        deferred_results=_deferred_results(),
        status="pending",
        completed=False,
    )
    agent = FakeAgent(triage_output=TriageDecision(action="answer", answer="hello"))
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
    key = _key()
    agent = FakeAgent(triage_output=TriageDecision(action="answer", answer="hello"))
    conversations = FakeConversationManager()
    im = _channel()
    markdown_feeler = RecordingMarkdownFeeler()
    im.feelers.markdown = markdown_feeler

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision.answer == "hello"
    assert markdown_feeler.calls == [(key, "hello")]
    assert im.sent == []


async def test_triage_graph_emits_reception_after_route() -> None:
    key = _key()
    agent = FakeAgent(
        triage_output=TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()
    ops = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im, "ops": ops},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.target.channel_id == "ops"
    assert result.target.mode == "sub"
    assert result.result is not None
    assert result.result.output == "done"
    assert [turn.run_name for turn in agent.turns] == ["triage"]
    assert agent.streams[0].key.thread_id == "hint-thread"
    assert agent.streams[0].run_name == "reception"
    assert agent.streams[0].prompt == "Please debug this in reception."
    # The triage graph no longer ensures conversations for runs; react loads
    # history from the (sub-thread) conversation itself.
    assert conversations.ensured == []
    assert im.sent == []
    assert ops.sub_threads[0][1] == "Working on it"
    assert ops.consumed[0][0].thread_id == "hint-thread"
    assert ops.sent[-1][2][0]["text"] == "done"


async def test_triage_resume_does_not_leak_deferred_results_into_reception() -> None:
    # A triage-phase question is answered, and the triage run then decides to
    # open a reception. The triage answer must NOT leak into that fresh
    # sub-thread run — it would resume an empty conversation and raise
    # "Tool call results were provided, but the message history is empty."
    key = _key()
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_key=key,
        target_key=key,
        requests=_requests(),
        deferred_results=deferred_results,
    )
    agent = FakeAgent(
        triage_output=TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
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
    key = _key()
    agent = FakeAgent(
        triage_output=TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()
    ops = _channel()

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key, user_prompt="debug this"),
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
    # Reception streamed through the target channel's consumer, not markdown.
    assert len(ops.consumed) == 1
    assert ops.consumed[0][0].thread_id == "hint-thread"
    assert ops.sent[-1][2][0]["text"] == "done"
    assert im.consumed == []
    assert im.sent == []


async def test_triage_graph_fails_fast_when_stream_produces_no_result() -> None:
    key = _key()
    agent = FakeAgent(
        triage_output=TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()
    ops = DroppingChannel(
        config=ChannelConfig(
            type="fake",
            agent_id="inkling",
            stream=ChannelStreamConfig(enabled=True),
        )
    )

    with pytest.raises(RuntimeError, match="completed without a result"):
        await triage_graph.run(
            RunTriage(),
            state=_state(key, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im, "ops": ops},
                agent=agent,
            ),
        )

    assert len(agent.streams) == 0


async def test_triage_graph_runs_final_reception_without_stream_when_disabled() -> None:
    key = _key()
    agent = FakeAgent(
        triage_output=TriageDecision(
            action="reception",
            target_id="im",
            reason="needs work",
            handoff="Please finish this without streaming.",
        ),
        reception_output="done",
        allow_reception_run=True,
    )
    conversations = FakeConversationManager()
    im = _channel(stream=False)

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key, user_prompt="debug this"),
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


async def test_triage_graph_skips_triage_inside_flat_thread() -> None:
    key = _key(thread_id="existing-thread")
    agent = FakeAgent(
        triage_output=TriageDecision(action="answer", answer="unused"),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = _channel()

    result = (
        await triage_graph.run(
            Route(),
            state=_state(key, user_prompt="continue"),
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
    assert agent.streams[0].key == key
    assert agent.streams[0].run_name == "reception"
    assert agent.streams[0].prompt == "continue"
    assert im.sub_threads == []
    assert im.consumed[0][0] == key
    assert im.sent[-1][2][0]["text"] == "done"


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
            key: ConversationKey,
            hint_text: str,
        ) -> ConversationKey:
            raise RuntimeError("platform refused the thread")

    key = _key()
    agent = FakeAgent(
        triage_output=TriageDecision(
            action="reception",
            target_id="im",
            reason="needs work",
            handoff="Please continue in reception.",
        ),
        reception_output="done",
    )
    conversations = FakeConversationManager()
    im = FailingSubThreadChannel(
        config=ChannelConfig(
            type="fake",
            agent_id="inkling",
            stream=ChannelStreamConfig(enabled=True),
        )
    )

    result = (
        await triage_graph.run(
            RunTriage(),
            state=_state(key, user_prompt="debug this"),
            deps=_deps(
                conversations=conversations,
                channels={"im": im},
                agent=agent,
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.target.mode == "main"
    # Reception ran at the main conversation key, not a sub-thread.
    assert im.consumed[0][0] == key
    assert agent.streams[0].key == key


async def test_resume_routes_reception_batch_to_run_reception() -> None:
    key = _key(thread_id="hint-thread")
    deferred_results = _deferred_results()
    batch = FakeDeferredBatch(
        source_key=_key(),
        target_key=key,
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
    assert agent.streams[0].key == key
    assert [stream.deferred_results for stream in agent.streams] == [deferred_results]
    assert im.consumed[0][0] == key
    # The resumed batch is marked resuming, then completed after the run.
    assert action_manager.marked == [
        (batch.id, "resuming", False),
        (batch.id, "completed", True),
    ]


async def test_resume_returns_triage_result_for_already_completed_batch() -> None:
    key = _key(thread_id="hint-thread")
    decision = TriageDecision(action="reception", target_id="im", reason="resumed")
    batch = FakeDeferredBatch(
        source_key=_key(),
        target_key=key,
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
    assert result.decision is decision
    assert agent.turns == []
    assert agent.streams == []
    assert action_manager.marked == []
    assert im.sent == []


async def test_resume_keeps_incomplete_reception_batch_deferred() -> None:
    key = _key(thread_id="hint-thread")
    batch = FakeDeferredBatch(
        source_key=_key(),
        target_key=key,
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
