from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent

from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.graph import (
    ResponseTarget,
    TriageDecision,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.tentacles.agent.graph.triage import RunTriage
from octomate.tentacles.channel.base import ChannelTentacle


@dataclass
class FakeAgent:
    decision: TriageDecision
    reception_output: str = "done"
    runs: list[str | None] = field(default_factory=list)
    streams: list[str | None] = field(default_factory=list)

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
            raise AssertionError("reception should use run_stream_events")
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
        self.streams.append(run_name)

        async def events() -> AsyncIterator[
            AgentStreamEvent | AgentRunResultEvent[Any]
        ]:
            yield AgentRunResultEvent(AgentRunResult(self.reception_output))

        yield events()


@dataclass
class FakeChannel:
    config: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
        )
    )
    sent: list[tuple[ConversationKey, list[str], list[MessageEvent]]] = field(
        default_factory=list
    )
    stream_sent: list[tuple[ConversationKey, list[str], list[MessageEvent]]] = field(
        default_factory=list
    )
    sub_threads: list[tuple[ConversationKey, str, list[MessageEvent]]] = field(
        default_factory=list
    )

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        outputs: list[str] = []
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(str(event.result.output))
        self.sent.append((key, outputs, list(source_events or [])))

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        outputs: list[str] = []
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(str(event.result.output))
        self.stream_sent.append((key, outputs, list(source_events or [])))

    async def respond_text(
        self,
        key: ConversationKey,
        text: str,
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        self.sent.append((key, [text], list(source_events or [])))

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> ConversationKey:
        self.sub_threads.append((key, hint_text, list(source_events or [])))
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


def _targets(key: ConversationKey) -> dict[str, ResponseTarget]:
    return {
        "im": ResponseTarget(
            id="im",
            channel_id="im",
            key=key,
            mode="main",
        ),
        "ops": ResponseTarget(
            id="ops",
            channel_id="ops",
            key=ConversationKey(
                channel_tentacle_id="ops",
                chat_type="private",
                chat_id="alice",
                user_id="alice",
            ),
            mode="main",
        ),
    }


async def test_triage_graph_emits_direct_route() -> None:
    key = _key()
    agent = FakeAgent(TriageDecision(action="answer", answer="hello"))
    im = FakeChannel()
    ops = FakeChannel()

    result = (
        await triage_graph.run(
            RunTriage(user_prompt="hi"),
            state=TriageState(),
            deps=TriageDeps(
                agent=cast(AgentTentacle, agent),
                conversation_key=key,
                targets=_targets(key),
                channels={
                    "im": cast(ChannelTentacle, im),
                    "ops": cast(ChannelTentacle, ops),
                },
                source_events=[],
                direct_target_id="im",
                reception_target_id="im",
            ),
        )
    ).output

    assert result.decision.answer == "hello"
    assert result.target.id == "im"
    assert result.result is None
    assert agent.runs == ["triage"]
    assert agent.streams == []
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
            title="Working on it",
        )
    )
    im = FakeChannel()
    ops = FakeChannel()

    result = (
        await triage_graph.run(
            RunTriage(user_prompt="debug this"),
            state=TriageState(),
            deps=TriageDeps(
                agent=cast(AgentTentacle, agent),
                conversation_key=key,
                targets=_targets(key),
                channels={
                    "im": cast(ChannelTentacle, im),
                    "ops": cast(ChannelTentacle, ops),
                },
                source_events=[],
                direct_target_id="im",
                reception_target_id="im",
            ),
        )
    ).output

    assert result.target.id == "ops"
    assert result.target.mode == "sub"
    assert result.result is not None
    assert result.result.output == "done"
    assert agent.runs == ["triage"]
    assert agent.streams == ["reception"]
    assert im.sent == []
    assert ops.sub_threads[0][1] == "Working on it"
    assert ops.stream_sent[0][0].thread_id == "hint-thread"
    assert ops.stream_sent[0][1] == ["done"]
