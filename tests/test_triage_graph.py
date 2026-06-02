from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import uuid
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
    ResponseTarget,
    ResponseTargetMode,
    TriageDecision,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.tentacles.agent.graph.triage import ResumeDeferredActions, RunTriage
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
    decision: TriageDecision | DeferredToolRequests
    id: str = "inkling"
    reception_output: str = "done"
    allow_reception_run: bool = False
    runs: list[str | None] = field(default_factory=list)
    run_prompts: list[str | Sequence[UserContent] | None] = field(default_factory=list)
    deferred_results: list[DeferredToolResults | None] = field(default_factory=list)
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
        instructions: str | None = None,
    ) -> AgentRunResult[FakeRunOutput]:
        self.runs.append(run_name)
        self.run_prompts.append(user_prompt)
        self.deferred_results.append(deferred_tool_results)
        if run_name == "reception":
            if not self.allow_reception_run:
                raise AssertionError("reception should use run_stream_events")
            return AgentRunResult(self.reception_output)
        return AgentRunResult(self.decision)

    @asynccontextmanager
    async def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> AsyncIterator[AsyncIterator[FakeStreamEvent]]:
        self.streams.append(
            (user_prompt, list(message_history or []), conversation_key, run_name)
        )

        async def events() -> AsyncIterator[FakeStreamEvent]:
            yield AgentRunResultEvent(AgentRunResult(self.reception_output))

        yield events()

    async def run_stream(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> AsyncIterator[StreamedRunResult[None, str]]:
        self.streams.append(
            (user_prompt, list(message_history or []), conversation_key, run_name)
        )
        yield StreamedRunResult([], 0, run_result=AgentRunResult(self.reception_output))


@dataclass
class RecordingMarkdownStreamRecorder:
    stream_sent: list[tuple[ConversationKey, list[str]]]

    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, str],
    ) -> str | None:
        output = await stream.get_output()
        outputs = [output]
        self.stream_sent.append((key, outputs))
        return "stream-message"


class NoopMarkdownStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, str],
    ) -> str | None:
        return None


class NoopEventStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[FakeStreamEvent],
    ) -> str | None:
        async for _event in events:
            pass
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
            markdown_stream=(
                RecordingMarkdownStreamRecorder(self.stream_sent)
                if self.config.stream.enabled
                else NoopMarkdownStreamFeeler()
            ),
            event_stream=(
                RecordingEventStreamRecorder(self.stream_sent)
                if self.config.stream.enabled
                else NoopEventStreamFeeler()
            ),
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
class RecordingMarkdownStreamFeeler:
    calls: list[tuple[ConversationKey, str]] = field(default_factory=list)

    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, str],
    ) -> str | None:
        output = await stream.get_output()
        self.calls.append((key, str(output)))
        return "stream-message"


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


def _key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )


def _source_target(key: ConversationKey) -> ResponseTarget:
    return ResponseTarget(
        channel_id="im",
        key=key,
        thread_strategy="flat_thread",
        mode="main",
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
                RunTriage(
                    agent=cast(AgentTentacle, agent),
                    source_target=_source_target(key),
                    user_prompt="hi",
                ),
                state=TriageState(),
                deps=TriageDeps(
                    conversation_manager=cast(ConversationManager, conversations),
                    channels={
                        "im": cast(ChannelTentacle, im),
                        "ops": cast(ChannelTentacle, ops),
                },
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
            RunTriage(
                agent=cast(AgentTentacle, agent),
                source_target=_source_target(key),
                user_prompt="hi",
            ),
            state=TriageState(),
            deps=TriageDeps(
                conversation_manager=cast(ConversationManager, conversations),
                action_manager=cast(DeferredActionManager, action_manager),
                channels={"im": cast(ChannelTentacle, im)},
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
            ResumeDeferredActions(
                awake=DeferredActionBatchResponse(batch_id=batch.id),
                agent=cast(AgentTentacle, agent),
                source_target=_source_target(key),
            ),
            state=TriageState(),
            deps=TriageDeps(
                conversation_manager=cast(ConversationManager, conversations),
                action_manager=cast(DeferredActionManager, action_manager),
                channels={"im": cast(ChannelTentacle, im)},
            ),
        )
    ).output

    assert not isinstance(result, DeferredResult)
    assert result.decision.answer == "hello"
    assert result.target == _source_target(key)
    assert agent.runs == ["triage"]
    assert agent.run_prompts == [None]
    assert agent.deferred_results == [deferred_results]
    assert conversations.ensured == [key]
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
            ResumeDeferredActions(
                awake=DeferredActionBatchResponse(batch_id=batch.id),
                agent=cast(AgentTentacle, agent),
                source_target=_source_target(key),
            ),
            state=TriageState(),
            deps=TriageDeps(
                conversation_manager=cast(ConversationManager, conversations),
                action_manager=cast(DeferredActionManager, action_manager),
                channels={"im": cast(ChannelTentacle, im)},
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
    stream_feeler = RecordingMarkdownStreamFeeler()
    markdown_feeler = RecordingMarkdownFeeler()
    im.feelers.markdown_stream = stream_feeler
    im.feelers.markdown = markdown_feeler

    result = (
        await triage_graph.run(
            RunTriage(
                agent=cast(AgentTentacle, agent),
                source_target=_source_target(key),
                user_prompt="hi",
            ),
            state=TriageState(),
            deps=TriageDeps(
                conversation_manager=cast(ConversationManager, conversations),
                channels={"im": cast(ChannelTentacle, im)},
            ),
        )
    ).output

    assert result.decision.answer == "hello"
    assert markdown_feeler.calls == [(key, "hello")]
    assert stream_feeler.calls == []
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
                RunTriage(
                    agent=cast(AgentTentacle, agent),
                    source_target=_source_target(key),
                    user_prompt="debug this",
                ),
                state=TriageState(),
                deps=TriageDeps(
                    conversation_manager=cast(ConversationManager, conversations),
                    channels={
                        "im": cast(ChannelTentacle, im),
                        "ops": cast(ChannelTentacle, ops),
                },
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
    assert conversations.ensured == [agent.streams[0][2]]
    assert im.sent == []
    assert ops.sub_threads[0][1] == "Working on it"
    assert ops.stream_sent[0][0].thread_id == "hint-thread"
    assert ops.stream_sent[0][1] == ["done"]


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
            RunTriage(
                agent=cast(AgentTentacle, agent),
                source_target=_source_target(key),
                user_prompt="debug this",
            ),
            state=TriageState(),
            deps=TriageDeps(
                conversation_manager=cast(ConversationManager, conversations),
                channels={
                    "im": cast(ChannelTentacle, im),
                    "ops": cast(ChannelTentacle, ops),
                },
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
            RunTriage(
                agent=cast(AgentTentacle, agent),
                source_target=_source_target(key),
                user_prompt="debug this",
            ),
            state=TriageState(),
            deps=TriageDeps(
                conversation_manager=cast(ConversationManager, conversations),
                channels={
                    "im": cast(ChannelTentacle, im),
                    "ops": cast(ChannelTentacle, ops),
                },
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
                RunTriage(
                    agent=cast(AgentTentacle, agent),
                    source_target=_source_target(key),
                    user_prompt="debug this",
                ),
                state=TriageState(),
                deps=TriageDeps(
                    conversation_manager=cast(ConversationManager, conversations),
                    channels={"im": cast(ChannelTentacle, im)},
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
