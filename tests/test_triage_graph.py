from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.output import OutputSpec
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolResults

from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.graph import (
    ResponseTarget,
    TriageDecision,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.tentacles.agent.graph.triage import RunTriage
from octomate.tentacles.channel.base import ChannelTentacle, ThreadStrategy
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)


FakeRunOutput = TriageDecision | str
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
    decision: TriageDecision
    id: str = "inkling"
    reception_output: str = "done"
    allow_reception_run: bool = False
    runs: list[str | None] = field(default_factory=list)
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
        output_type: OutputSpec[TriageDecision] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        instructions: str | None = None,
    ) -> AgentRunResult[FakeRunOutput]:
        self.runs.append(run_name)
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
    assert stream_feeler.calls == []
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
