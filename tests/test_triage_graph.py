from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent

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
        message_history: Sequence[ModelMessage] | None = None,
        **kwargs: Any,
    ) -> AgentRunResult[Any]:
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
        **kwargs: Any,
    ) -> AsyncIterator[AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]]]:
        self.streams.append(
            (user_prompt, list(message_history or []), conversation_key, run_name)
        )

        async def events() -> AsyncIterator[
            AgentStreamEvent | AgentRunResultEvent[Any]
        ]:
            yield AgentRunResultEvent(AgentRunResult(self.reception_output))

        yield events()


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

    async def respond(
        self,
        key: ConversationKey,
        result: AgentRunResult[Any],
    ) -> None:
        self.sent.append((key, [str(result.output)]))

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
    ) -> None:
        outputs: list[str] = []
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(str(event.result.output))
        self.stream_sent.append((key, outputs))

    async def respond_text(
        self,
        key: ConversationKey,
        text: str,
    ) -> None:
        self.sent.append((key, [text]))

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
            RunTriage(user_prompt="hi"),
            state=TriageState(),
            deps=TriageDeps(
                agent=cast(AgentTentacle, agent),
                conversation_manager=cast(ConversationManager, conversations),
                source_target=_source_target(key),
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
            RunTriage(user_prompt="debug this"),
            state=TriageState(),
            deps=TriageDeps(
                agent=cast(AgentTentacle, agent),
                conversation_manager=cast(ConversationManager, conversations),
                source_target=_source_target(key),
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
            RunTriage(user_prompt="debug this"),
            state=TriageState(),
            deps=TriageDeps(
                agent=cast(AgentTentacle, agent),
                conversation_manager=cast(ConversationManager, conversations),
                source_target=_source_target(key),
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
