from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, ToolCallPart, UserContent
from pydantic_ai.output import OutputSpec
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.graph import (
    DeferredResult,
    DeferredSuspender,
    ResponseTarget,
    ResponseTargetMode,
    TriageDecision,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.tentacles.agent.graph.triage import ResumeDeferred, RunTriage
from octomate.tentacles.channel.base import ChannelTentacle, ThreadStrategy
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)


FakeRunOutput = TriageDecision | str | DeferredToolRequests
FakeStreamEvent = AgentStreamEvent | AgentRunResultEvent[str]


@dataclass
class FakeConversation:
    messages: list[ModelMessage] = field(default_factory=list)


@dataclass
class FakeConversationManager:
    conversations: dict[ConversationKey, FakeConversation] = field(
        default_factory=dict
    )
    ensured: list[ConversationKey] = field(default_factory=list)

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str | None = None,
    ) -> FakeConversation:
        self.ensured.append(key)
        conversation = self.conversations.get(key)
        if conversation is None:
            conversation = FakeConversation()
            self.conversations[key] = conversation
        return conversation


@dataclass
class FakeAgent:
    """Stands in for an AgentTentacle. Because the react graph is short-circuited
    here, the fake itself honors the AgentTentacle contract: when its output is a
    DeferredToolRequests it invokes the supplied suspender, exactly as react would.
    """

    decision: TriageDecision | DeferredToolRequests
    id: str = "inkling"
    reception_output: str = "done"
    allow_reception_run: bool = False
    runs: list[str | None] = field(default_factory=list)
    run_prompts: list[str | Sequence[UserContent] | None] = field(default_factory=list)
    deferred_results: list[DeferredToolResults | None] = field(default_factory=list)
    stream_deferred: list[DeferredToolResults | None] = field(default_factory=list)
    message_histories: list[list[ModelMessage]] = field(default_factory=list)
    streams: list[
        tuple[
            str | Sequence[UserContent] | None,
            list[ModelMessage],
            ConversationKey,
            str | None,
        ]
    ] = field(default_factory=list)

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[FakeRunOutput] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        instructions: str | None = None,
    ) -> AgentRunResult[FakeRunOutput]:
        self.runs.append(run_name)
        self.run_prompts.append(user_prompt)
        self.deferred_results.append(deferred_tool_results)
        self.message_histories.append(list(message_history or []))
        if run_name == "reception":
            if not self.allow_reception_run:
                raise AssertionError("reception should use run_stream_events")
            output: FakeRunOutput = self.reception_output
        else:
            output = self.decision
        if isinstance(output, DeferredToolRequests) and deferred_suspender is not None:
            await deferred_suspender.suspend(output)
        return AgentRunResult(output)

    @asynccontextmanager
    async def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
    ) -> AsyncIterator[AsyncIterator[FakeStreamEvent]]:
        self.streams.append(
            (user_prompt, list(message_history or []), conversation_key, run_name)
        )
        self.stream_deferred.append(deferred_tool_results)

        async def events() -> AsyncIterator[FakeStreamEvent]:
            yield AgentRunResultEvent(AgentRunResult(self.reception_output))

        yield events()


class NoopMarkdownStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, str],
    ) -> str | None:
        return None


@dataclass
class RecordingEventStreamRecorder:
    stream_sent: list[tuple[ConversationKey, list[str]]]

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[FakeStreamEvent],
    ) -> str | None:
        outputs: list[str] = []
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(str(event.result.output))
        self.stream_sent.append((key, outputs))
        return "event-message"


@dataclass
class FakeChannel:
    thread_strategy: ThreadStrategy = "flat_thread"
    config: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
        )
    )
    sent: list[tuple[ConversationKey, list[str]]] = field(default_factory=list)
    stream_sent: list[tuple[ConversationKey, list[str]]] = field(default_factory=list)
    sub_threads: list[tuple[ConversationKey, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.feelers = Feelers[str](
            markdown=self,
            markdown_stream=NoopMarkdownStreamFeeler(),
            event_stream=RecordingEventStreamRecorder(self.stream_sent),
            approvals=PlainTextApprovalFeeler(self),
            ask_questions=PlainTextAskQuestionFeeler(self),
        )

    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> str | None:
        self.sent.append((key, [markdown]))
        return None

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        self.sub_threads.append((key, hint_text))
        return ConversationKey(
            channel_tentacle_id=key.channel_tentacle_id,
            chat_type=key.chat_type,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id="hint-thread",
        )


@dataclass
class RecordingMarkdownFeeler:
    calls: list[tuple[ConversationKey, str]] = field(default_factory=list)

    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> str | None:
        self.calls.append((key, markdown))
        return "markdown-message"


@dataclass
class RecordingEventStreamFeeler:
    calls: list[tuple[ConversationKey, str]] = field(default_factory=list)

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[FakeStreamEvent],
    ) -> str | None:
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                self.calls.append((key, str(event.result.output)))
        return "event-message"


class DroppingEventStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[FakeStreamEvent],
    ) -> str | None:
        return None


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
    channels: dict[str, FakeChannel],
    agent: FakeAgent,
    action_manager: FakeActionManager | None = None,
) -> TriageDeps:
    return TriageDeps(
        channels={cid: cast(ChannelTentacle, c) for cid, c in channels.items()},
        agents={agent.id: cast(AgentTentacle, agent)},
        conversation_manager=cast(ConversationManager, conversations),
        action_manager=cast(DeferredActionManager, action_manager or FakeActionManager()),
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


@dataclass
class CreateBatchCall:
    conversation: FakeConversation
    agent_tentacle_id: str
    run_name: str | None
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode
    decision: TriageDecision | None
    requests: DeferredToolRequests


@dataclass
class FakePresentedBatch:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    questions: list[DeferredQuestion] = field(default_factory=list)
    approvals: list[DeferredApproval] = field(default_factory=list)


@dataclass
class FakeDeferredBatch:
    source_key: ConversationKey
    target_key: ConversationKey
    requests: DeferredToolRequests
    deferred_results: DeferredToolResults
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    agent_tentacle_id: str = "inkling"
    run_name: str | None = "triage"
    target_mode: ResponseTargetMode = "main"
    decision: TriageDecision | None = None
    status: str = "resolved"
    completed: bool = True

    def build_results(self) -> DeferredToolResults:
        return self.deferred_results


@dataclass
class FakeActionManager:
    batch: FakeDeferredBatch | None = None
    create_calls: list[CreateBatchCall] = field(default_factory=list)
    presented: list[tuple[uuid.UUID, str | None]] = field(default_factory=list)
    marked: list[tuple[uuid.UUID, str, bool]] = field(default_factory=list)

    async def create_batch(
        self,
        *,
        conversation: FakeConversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> FakePresentedBatch:
        self.create_calls.append(
            CreateBatchCall(
                conversation=conversation,
                agent_tentacle_id=agent_tentacle_id,
                run_name=run_name,
                source_key=source_key,
                target_key=target_key,
                target_mode=target_mode,
                decision=decision,
                requests=requests,
            )
        )
        return FakePresentedBatch()

    async def mark_action_presented(
        self,
        action_id: uuid.UUID,
        platform_message_id: str | None,
    ) -> None:
        self.presented.append((action_id, platform_message_id))

    async def resolve_batch(
        self,
        awake: DeferredActionBatchResponse,
    ) -> FakeDeferredBatch:
        if self.batch is None:
            raise ValueError(f"unknown deferred action batch {awake.batch_id}")
        return self.batch

    async def get_batch(self, batch_id: uuid.UUID) -> FakeDeferredBatch:
        if self.batch is None:
            raise ValueError(f"unknown deferred action batch {batch_id}")
        return self.batch

    async def mark_batch(
        self,
        batch_id: uuid.UUID,
        status: str,
        *,
        completed: bool = False,
    ) -> None:
        self.marked.append((batch_id, status, completed))


async def test_triage_graph_emits_direct_route() -> None:
    key = _key()
    agent = FakeAgent(TriageDecision(action="answer", answer="hello"))
    conversations = FakeConversationManager()
    im = FakeChannel()
    ops = FakeChannel()

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

    assert result.decision.answer == "hello"
    assert result.target.channel_id == "im"
    assert result.result is None
    assert agent.runs == ["triage"]
    assert agent.streams == []
    assert conversations.ensured == []
    assert im.sent[0][1] == ["hello"]
    assert im.stream_sent == []
    assert ops.sent == []


async def test_triage_graph_returns_deferred_result_when_triage_requests_input() -> None:
    key = _key()
    requests = _requests()
    agent = FakeAgent(requests)
    conversations = FakeConversationManager()
    action_manager = FakeActionManager()
    im = FakeChannel()

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
    assert agent.runs == ["triage"]
    assert agent.run_prompts == ["hi"]
    assert conversations.ensured == [key]
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
    agent = FakeAgent(TriageDecision(action="answer", answer="hello"))
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = FakeChannel()

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
    assert agent.runs == ["triage"]
    assert agent.run_prompts == [None]
    assert agent.deferred_results == [deferred_results]
    # RunTriage no longer threads history through the graph — react loads it
    # from the conversation (which carries the deferred turn the create run
    # recorded), so the agent is called with no explicit history.
    assert agent.message_histories == [[]]
    # ResumeDeferred no longer touches the conversation; history is loaded by
    # react when it runs, which the fake agent here bypasses.
    assert conversations.ensured == []
    assert action_manager.marked == [
        (batch.id, "resuming", False),
        (batch.id, "completed", True),
    ]
    assert im.sent == [(key, ["hello"])]


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
    agent = FakeAgent(TriageDecision(action="answer", answer="hello"))
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = FakeChannel()

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
    assert agent.runs == []
    assert conversations.ensured == []
    assert action_manager.marked == []
    assert im.sent == []


async def test_triage_graph_uses_markdown_feeler_for_direct_answer() -> None:
    key = _key()
    agent = FakeAgent(TriageDecision(action="answer", answer="hello"))
    conversations = FakeConversationManager()
    im = FakeChannel()
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

    assert result.decision.answer == "hello"
    assert markdown_feeler.calls == [(key, "hello")]
    assert im.sent == []


async def test_triage_graph_emits_reception_after_route() -> None:
    key = _key()
    agent = FakeAgent(
        TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        )
    )
    conversations = FakeConversationManager()
    im = FakeChannel()
    ops = FakeChannel()

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

    assert result.target.channel_id == "ops"
    assert result.target.mode == "sub"
    assert result.result is not None
    assert result.result.output == "done"
    assert agent.runs == ["triage"]
    assert agent.streams[0][2].thread_id == "hint-thread"
    assert agent.streams[0][3] == "reception"
    assert agent.streams[0][0] == "Please debug this in reception."
    # The triage graph no longer ensures conversations for runs; react loads
    # history from the (sub-thread) conversation itself.
    assert conversations.ensured == []
    assert im.sent == []
    assert ops.sub_threads[0][1] == "Working on it"
    assert ops.stream_sent[0][0].thread_id == "hint-thread"
    assert ops.stream_sent[0][1] == ["done"]


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
        TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        )
    )
    conversations = FakeConversationManager()
    action_manager = FakeActionManager(batch=batch)
    im = FakeChannel()
    ops = FakeChannel()

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
    assert agent.runs == ["triage"]
    assert agent.deferred_results == [deferred_results]
    # ... and the following reception ran fresh, WITHOUT the leaked answer.
    assert agent.streams[0][3] == "reception"
    assert agent.stream_deferred == [None]


async def test_triage_graph_uses_event_stream_feeler_for_reception() -> None:
    key = _key()
    agent = FakeAgent(
        TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        )
    )
    conversations = FakeConversationManager()
    im = FakeChannel()
    ops = FakeChannel()
    event_feeler = RecordingEventStreamFeeler()
    ops.feelers.event_stream = event_feeler

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

    assert result.result is not None
    assert result.result.output == "done"
    assert len(event_feeler.calls) == 1
    call_key, output = event_feeler.calls[0]
    assert call_key.thread_id == "hint-thread"
    assert output == "done"
    assert ops.stream_sent == []


async def test_triage_graph_fails_fast_when_stream_produces_no_result() -> None:
    key = _key()
    agent = FakeAgent(
        TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please debug this in reception.",
        )
    )
    conversations = FakeConversationManager()
    im = FakeChannel()
    ops = FakeChannel()
    ops.feelers.event_stream = DroppingEventStreamFeeler()

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
        TriageDecision(
            action="reception",
            target_id="im",
            reason="needs work",
            handoff="Please finish this without streaming.",
        ),
        allow_reception_run=True,
    )
    conversations = FakeConversationManager()
    im = FakeChannel(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
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

    assert result.result is not None
    assert result.result.output == "done"
    assert agent.runs == ["triage", "reception"]
    assert agent.streams == []
    assert im.sub_threads[0][1] == "needs work"
    assert im.sent[0][0].thread_id == "hint-thread"
    assert im.sent[0][1] == ["done"]


async def test_triage_graph_skips_triage_inside_flat_thread() -> None:
    key = _key(thread_id="existing-thread")
    agent = FakeAgent(TriageDecision(action="answer", answer="unused"))
    conversations = FakeConversationManager()
    im = FakeChannel()

    from octomate.tentacles.agent.graph.triage import Route

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

    assert result.target.channel_id == "im"
    assert result.target.mode == "sub"
    assert result.result is not None
    assert result.result.output == "done"
    assert agent.runs == []
    assert agent.streams[0][2] == key
    assert agent.streams[0][3] == "reception"
    assert agent.streams[0][0] == "continue"
    assert im.sub_threads == []
    assert im.stream_sent[0][0] == key
    assert im.stream_sent[0][1] == ["done"]
