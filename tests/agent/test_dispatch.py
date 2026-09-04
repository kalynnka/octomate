"""Octomate.kick end-to-end under the collapsed dispatch: signal → Awake → Route →
one self-routing reception run (which may summon or teleport) → channel rendering,
over the real ConversationManager/ThreadManager on in-memory SQLite."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.capabilities.harness.agent import Agent
from octomate.config import AgentModelConfig, ChannelConfig, ChannelStreamConfig
from octomate.database import async_session
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadMessage
from octomate.schemas.triage import (
    Claim,
    HereLanding,
    SummonDecision,
    ThreadLanding,
)
from octomate.tentacles.agents.inkling import InklingTentacle
from octomate.tentacles.agents.inkling.base import InklingOutput
from octomate.tentacles.base import Tentacle
from tests.support.agents import FakeAgent
from tests.support.channels import (
    FakeChannelTentacle,
    MainOnlyChannelTentacle,
    NativeMessage,
    RecordingInk,
)
from tests.support.managers import a_loaded_thread
from tests.support.scenarios import mid_run_notice


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def _entry_config(*, stream: bool, entry: str = "inkling") -> ChannelConfig:
    """The entry (default) agent is receptions[0]; extra receptions are summon routes."""
    return ChannelConfig(
        type="fake",
        stream=ChannelStreamConfig(enabled=stream),
        agents=[AgentModelConfig(agent=entry, model="test")],
    )


def _summon_config(*, stream: bool) -> ChannelConfig:
    return ChannelConfig(
        type="fake",
        stream=ChannelStreamConfig(enabled=stream),
        agents=[
            AgentModelConfig(agent="inkling", model="test"),
            AgentModelConfig(agent="claude", model="opus"),
        ],
    )


def _event(
    *,
    tentacle_id: str = "im",
    message_id: str = "m1",
    user_id: str = "alice",
    text: str = "hi",
    thread_id: str = "",
    chat_type: ChatType = "dm",
) -> MessageEvent:
    return MessageEvent(
        tentacle_id=tentacle_id,
        message_id=message_id,
        chat_type=chat_type,
        chat_id="team" if chat_type == "group" else "alice",
        user_id=user_id,
        channel_thread_id=thread_id,
        segments=[TextSegment(data={"text": text})],
    )


async def _agent_rows(thread_id: uuid.UUID, text: str) -> list[ThreadMessage]:
    """The agent's own rows in a thread, read off the ledger: what reception
    recorded, before anyone's history tools come to search it."""
    async with async_session() as session:
        rows = await session.list(
            ThreadMessage,
            limit=None,
            expressions=[
                ThreadMessage["thread_id"] == thread_id,
                ThreadMessage["actor_kind"] == "agent",
                ThreadMessage["message_text"].ilike(f"%{text}%"),
            ],
        )
    return list(rows)


def _key(thread_id: str = "") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
        channel_thread_id=thread_id,
    )


def _register_agents(octomate: Octomate, *agents: FakeAgent) -> None:
    for agent in agents:
        octomate.connect(agent)


class FailingMarkdownFeeler:
    async def present(self, address: ChannelAddress, markdown: str) -> str | None:
        raise RuntimeError("channel presentation failed")


class FailingSendInk(RecordingInk):
    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[NativeMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        raise RuntimeError("timeline presentation failed")


async def test_entry_agent_answers_in_one_run_without_claiming_ownership() -> None:
    # The collapsed dispatch: the channel default agent runs once (react) and
    # answers — no separate triage screen, and a plain answer pins no owner.
    octomate = Octomate()
    entry = FakeAgent(reception_output="handled", allow_reception_run=True)
    channel = FakeChannelTentacle(config=_entry_config(stream=False))
    _register_agents(octomate, entry)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="answer me")]))

    assert [turn.run_name for turn in entry.turns] == ["react"]
    assert entry.streams == []
    assert channel.sent[0][2][0]["text"] == "handled"

    thread = await octomate.thread_manager.ensure(_key())
    assert list(thread.handoffs) == []
    assert thread.active_agent_tentacle_id is None


async def test_entry_agent_summons_into_a_sub_thread() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_summon=SummonDecision(
            action="summon",
            agent_id="claude",
            model="opus",
            destination=ThreadLanding(),
            reason="needs code work",
            hint="Working on it",
            summon="Please debug this.",
        ),
        allow_reception_run=True,
    )
    claude = FakeAgent(
        id="claude", reception_output="debugged", allow_reception_run=True
    )
    channel = FakeChannelTentacle(config=_summon_config(stream=False))
    _register_agents(octomate, entry, claude)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please debug")]))

    assert [turn.prompt for turn in claude.turns] == ["Please debug this."]
    assert channel.sub_threads[0][1] == "Working on it"
    handoff_thread = await octomate.thread_manager.ensure(_key(thread_id="hint-thread"))
    assert handoff_thread.active_agent_tentacle_id == "claude"
    assert channel.sent[-1][2][0]["text"] == "debugged"


async def test_summon_here_transmits_current_dm_ownership() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_summon=SummonDecision(
            action="summon",
            agent_id="claude",
            model="opus",
            destination=HereLanding(),
            reason="you own this DM now",
            hint="Taking over",
            summon="Continue with the user directly.",
        ),
        allow_reception_run=True,
    )
    claude = FakeAgent(
        id="claude", reception_output="took over", allow_reception_run=True
    )
    channel = FakeChannelTentacle(config=_summon_config(stream=False))
    _register_agents(octomate, entry, claude)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="hand this to claude")]))

    # No new surface — claude took over the current DM in place and now owns it.
    assert channel.sub_threads == []
    main_thread = await octomate.thread_manager.ensure(_key())
    assert main_thread.active_agent_tentacle_id == "claude"
    assert claude.turns[0].address == _key()
    assert channel.sent[-1][2][0]["text"] == "took over"


async def test_owned_thread_follow_up_skips_the_entry_agent() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_summon=SummonDecision(
            action="summon",
            agent_id="claude",
            model="opus",
            destination=ThreadLanding(),
            reason="needs code work",
            hint="Working on it",
            summon="Please debug this.",
        ),
    )
    claude = FakeAgent(id="claude")
    channel = FakeChannelTentacle(config=_summon_config(stream=True))
    _register_agents(octomate, entry, claude)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please debug")]))
    # A follow-up in the handoff thread routes straight to the owner, no entry run.
    await octomate.kick(
        UserMessageSignal([_event(text="more detail", thread_id="hint-thread")])
    )

    handoff_address = _key(thread_id="hint-thread")
    handoff_thread = await octomate.thread_manager.ensure(handoff_address)
    assert handoff_thread.active_agent_tentacle_id == "claude"
    assert len(entry.streams) == 1  # the entry agent ran only on the first turn
    assert entry.turns == []
    assert [stream.run_name for stream in claude.streams] == ["summon", "react"]
    assert claude.streams[1].address == handoff_address


async def test_owner_survives_cold_manager_reload() -> None:
    def _build() -> tuple[Octomate, FakeAgent, FakeAgent]:
        octomate = Octomate()
        entry = FakeAgent(
            reception_summon=SummonDecision(
                action="summon",
                agent_id="claude",
                model="opus",
                destination=ThreadLanding(),
                reason="needs code work",
                hint="Working on it",
                summon="Please debug this.",
            ),
        )
        claude = FakeAgent(id="claude")
        channel = FakeChannelTentacle(config=_summon_config(stream=True))
        _register_agents(octomate, entry, claude)
        octomate.connect(channel)
        return octomate, entry, claude

    first, _first_entry, first_claude = _build()
    await first.kick(UserMessageSignal([_event(text="please debug")]))
    assert [stream.run_name for stream in first_claude.streams] == ["summon"]

    # Fresh managers over the same DB: ownership reloads from the persisted
    # handoff, so the follow-up routes to Claude without a new entry run.
    second, second_entry, second_claude = _build()
    await second.kick(
        UserMessageSignal([_event(text="more detail", thread_id="hint-thread")])
    )

    assert second_entry.turns == []
    assert second_entry.streams == []
    assert [stream.run_name for stream in second_claude.streams] == ["react"]
    reloaded = await second.thread_manager.ensure(_key(thread_id="hint-thread"))
    assert reloaded.active_agent_tentacle_id == "claude"


async def test_chained_summon_updates_thread_owner() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_summon=SummonDecision(
            action="summon",
            agent_id="first",
            model="test",
            destination=ThreadLanding(),
            reason="needs first pass",
            hint="First pass",
            summon="First agent brief.",
        ),
        allow_reception_run=True,
    )
    first = FakeAgent(
        id="first",
        reception_summon=SummonDecision(
            action="summon",
            agent_id="second",
            model="test",
            destination=HereLanding(),
            reason="needs second pass",
            hint="Second pass",
            summon="Second agent brief.",
        ),
        allow_reception_run=True,
    )
    second = FakeAgent(id="second", reception_output="done", allow_reception_run=True)
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            agents=[
                AgentModelConfig(agent="inkling", model="test"),
                AgentModelConfig(agent="first", model="test"),
                AgentModelConfig(agent="second", model="test"),
            ],
        )
    )
    _register_agents(octomate, entry, first, second)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please debug")]))

    handoff_thread = await octomate.thread_manager.ensure(_key(thread_id="hint-thread"))
    assert [handoff.to_agent_tentacle_id for handoff in handoff_thread.handoffs] == [
        "first",
        "second",
    ]
    assert handoff_thread.active_agent_tentacle_id == "second"
    assert [turn.prompt for turn in first.turns] == ["First agent brief."]
    assert [turn.prompt for turn in second.turns] == ["Second agent brief."]


async def test_teleport_forks_history_into_a_sub_thread_and_resumes() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_teleport="Let's move to a thread",
        reception_output="continued in the thread",
        allow_reception_run=True,
    )
    channel = FakeChannelTentacle(config=_entry_config(stream=False))
    _register_agents(octomate, entry)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="do the multi-step thing")]))

    # A sub-thread was opened, and the agent resumed there and delivered its answer.
    assert channel.sub_threads[0][1] == "Let's move to a thread"
    thread_address = _key(thread_id="hint-thread")
    assert entry.turns[-1].address == thread_address
    assert channel.sent[-1][3] == "hint-thread"
    assert channel.sent[-1][2][0]["text"] == "continued in the thread"
    # (fork's history copy is unit-tested in test_conversation_manager; the fake
    # agent short-circuits react, so it records no messages to fork here.)


async def test_teleport_on_main_only_channel_stays_put() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_teleport="try to move",
        reception_output="stayed here",
        allow_reception_run=True,
    )
    channel = MainOnlyChannelTentacle(config=_entry_config(stream=False))
    _register_agents(octomate, entry)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="do it")]))

    assert channel.sub_threads == []
    assert entry.turns[-1].address == _key()
    assert channel.sent[-1][2][0]["text"] == "stayed here"


async def test_summon_here_keeps_reception_in_main_for_main_only_channel() -> None:
    # A channel that opens no sub-thread refuses `thread` at the gate rather than
    # landing the handoff in main behind the model's back, so `here` is the
    # destination that gets a summon anywhere on one. In a DM it is always allowed.
    octomate = Octomate()
    entry = FakeAgent(
        reception_summon=SummonDecision(
            action="summon",
            agent_id="claude",
            model="opus",
            destination=HereLanding(),
            reason="needs work",
            hint="needs work",
            summon="Please investigate this in main.",
        ),
    )
    channel = MainOnlyChannelTentacle(config=_summon_config(stream=True))
    claude = FakeAgent(id="claude")
    _register_agents(octomate, entry, claude)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please investigate")]))

    assert channel.sub_threads == []
    assert claude.streams[0].address == _key()
    assert claude.streams[0].prompt == "Please investigate this in main."


async def test_entry_runs_in_place_inside_a_flat_thread() -> None:
    octomate = Octomate()
    entry = FakeAgent()
    channel = FakeChannelTentacle(config=_entry_config(stream=True))
    _register_agents(octomate, entry)
    octomate.connect(channel)

    address = _key(thread_id="existing-thread")
    await octomate.kick(
        UserMessageSignal([_event(text="continue", thread_id="existing-thread")])
    )

    assert [stream.run_name for stream in entry.streams] == ["react"]
    assert entry.streams[0].address == address
    assert channel.sub_threads == []
    assert channel.consumed[0][0] == address
    assert channel.sent[-1][2][0]["text"] == "handled"


async def test_reception_model_is_resolved_from_agent() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_summon=SummonDecision(
            action="summon",
            agent_id="claude",
            model="openai:gpt-4o-mini",
            destination=ThreadLanding(),
            reason="needs stronger model",
            hint="needs stronger model",
            summon="Use the stronger model.",
        ),
    )
    claude = FakeAgent(
        id="claude",
        models={"openai:gpt-4o-mini": "pro-m"},
        claims={"openai:gpt-4o-mini": Claim(ability="fake agent")},
    )
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            agents=[
                AgentModelConfig(agent="inkling", model="test"),
                AgentModelConfig(agent="claude", model="openai:gpt-4o-mini"),
            ],
        )
    )
    _register_agents(octomate, entry, claude)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please work")]))

    assert claude.streams[0].model == "pro-m"


async def test_streamed_reception_records_output_without_timeline_source() -> None:
    octomate = Octomate()
    entry = FakeAgent(
        reception_script=mid_run_notice(notice="first notice", answer="final answer"),
    )
    channel = FakeChannelTentacle(config=_entry_config(stream=True))
    _register_agents(octomate, entry)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event()]))

    target_address = channel.consumed[0][0]
    thread = await a_loaded_thread(octomate.thread_manager, target_address)
    outbounds = [m for m in thread.messages if m.direction == "outbound"]
    assert [m.message_text for m in outbounds] == ["final answer"]


async def test_streamed_reception_persists_when_presentation_fails() -> None:
    octomate = Octomate()
    entry = FakeAgent(reception_output="survives render failure")
    channel = FakeChannelTentacle(
        ink=FailingSendInk(), config=_entry_config(stream=True)
    )
    _register_agents(octomate, entry)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event()]))

    thread = await octomate.thread_manager.ensure(_key())
    chat_messages = await _agent_rows(thread.id, "survives render failure")
    assert len(chat_messages) == 1
    assert chat_messages[0].platform_message_id is None


async def test_kick_builds_prompt_from_pending_thread_messages() -> None:
    octomate = Octomate()
    entry = FakeAgent(reception_output="handled", allow_reception_run=True)
    channel = FakeChannelTentacle(config=_entry_config(stream=False))
    _register_agents(octomate, entry)
    octomate.connect(channel)

    events = [
        _event(message_id="m1", text="first detail"),
        _event(message_id="m2", user_id="bob", text="second detail"),
        _event(message_id="m3", text="wake now"),
    ]
    stored = [await octomate.thread_manager.record_inbound(event) for event in events]

    await octomate.kick(
        UserMessageSignal([events[-1]], trigger_thread_message_id=stored[-1].id)
    )

    assert entry.turns[0].prompt == (
        "anonymous (alice) #msg:m1:\nfirst detail\n\n"
        "anonymous (bob) #msg:m2:\nsecond detail\n\n"
        "anonymous (alice) #msg:m3:\nwake now"
    )
    assert entry.turns[0].source_thread_message_ids == [m.id for m in stored]


def _inkling(octomate: Octomate, stream_text: str) -> InklingTentacle:
    async def stream(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> AsyncIterator[str]:
        yield stream_text

    model = FunctionModel(stream_function=stream, model_name="scripted")
    return InklingTentacle(
        "inkling",
        octomate,
        agent=cast(
            Agent[None, InklingOutput],
            Agent(
                model,
                deps_type=type(None),
                output_type=[str],
                capabilities=[AskCapability()],
            ),
        ),
        models={"test": model},
        conversation_manager=octomate.conversations,
    )


async def test_reception_records_and_binds_outbound_thread_message() -> None:
    octomate = Octomate()
    agent = _inkling(octomate, "all done!")
    channel = FakeChannelTentacle(config=_entry_config(stream=False))
    octomate.connect(agent)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please answer")]))

    thread = await octomate.thread_manager.ensure(_key())
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    model_message = (
        await octomate.conversations.search_messages(
            conversation.id, "all done", role="assistant"
        )
    )[0]
    chat_messages = await _agent_rows(thread.id, "all done")
    async with async_session() as session:
        stored_message = await session.one_or_none(
            ThreadMessage,
            expressions=[ThreadMessage["id"] == chat_messages[0].id],
        )
        assert stored_message is not None
        await stored_message.model_messages

    assert channel.sent[0][2][0]["text"] == "all done!"
    assert chat_messages[0].platform_message_id == "sent-1"
    assert stored_message.model_messages[0].id == model_message.id


async def test_reception_persists_before_channel_presentation() -> None:
    octomate = Octomate()
    agent = _inkling(octomate, "still persisted")
    channel = FakeChannelTentacle(config=_entry_config(stream=False))
    channel.feelers.markdown = FailingMarkdownFeeler()
    octomate.connect(agent)
    octomate.connect(channel)

    with pytest.raises(RuntimeError, match="channel presentation failed"):
        await octomate.kick(UserMessageSignal([_event(text="please answer")]))

    thread = await octomate.thread_manager.ensure(_key())
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    model_message = (
        await octomate.conversations.search_messages(
            conversation.id, "still persisted", role="assistant"
        )
    )[0]
    chat_messages = await _agent_rows(thread.id, "still persisted")
    assert len(chat_messages) == 1
    assert chat_messages[0].platform_message_id is None
    assert model_message is not None


async def test_connect_rejects_duplicates() -> None:
    octomate = Octomate()
    octomate.connect(FakeAgent())
    octomate.connect(FakeChannelTentacle())

    with pytest.raises(ValueError, match="agent 'inkling' already connected"):
        octomate.connect(FakeAgent())
    with pytest.raises(ValueError, match="channel 'im' already connected"):
        octomate.connect(FakeChannelTentacle())


def test_connect_skips_unknown_tentacles(caplog: pytest.LogCaptureFixture) -> None:
    octomate = Octomate()
    original = Octomate()
    tentacle = Tentacle("unknown", original)

    with caplog.at_level("WARNING", logger="octomate.base"):
        result = octomate.connect(tentacle)

    assert result is tentacle
    assert tentacle.octomate is original
    assert octomate.agents == {}
    assert octomate.channels == {}
    assert "Skipping unknown tentacle unknown" in caplog.text
