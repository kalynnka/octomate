"""Octomate.kick end-to-end: signal → triage graph → channel rendering, with
the real ConversationManager/DeferredActionManager over in-memory SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import AgentModelConfig, ChannelConfig, ChannelStreamConfig
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.triage import DirectAnswerDecision, SummonDecision
from octomate.tentacles.base import Tentacle
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle, MainOnlyChannelTentacle


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def _streaming_config() -> ChannelConfig:
    return ChannelConfig(
        type="fake",
        stream=ChannelStreamConfig(enabled=True),
        receptions=[AgentModelConfig(agent="other", model="test")],
    )


def _non_streaming_config() -> ChannelConfig:
    return ChannelConfig(
        type="fake",
        stream=ChannelStreamConfig(enabled=False),
        receptions=[AgentModelConfig(agent="other", model="test")],
    )


def _event(
    *,
    tentacle_id: str = "im",
    message_id: str = "m1",
    user_id: str = "alice",
    text: str = "hi",
    thread_id: str = "",
) -> MessageEvent:
    return MessageEvent(
        tentacle_id=tentacle_id,
        message_id=message_id,
        chat_type="private",
        chat_id="alice",
        user_id=user_id,
        thread_id=thread_id,
        segments=[TextSegment(data={"text": text})],
    )


def _key(thread_id: str = "") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id=thread_id,
    )


def _register_agents(octomate: Octomate, *agents: FakeAgent) -> None:
    for agent in agents:
        octomate.connect(agent)


async def test_octomate_kick_dispatches_directly_to_registered_agent() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="test",
            reason="needs work",
            hint="needs work",
            summon="Please handle this.",
        )
    )
    reception = FakeAgent(id="other", allow_reception_run=True)
    channel = FakeChannelTentacle(config=_non_streaming_config())
    _register_agents(octomate, agent, reception)
    octomate.connect(channel)

    event = _event()
    address = _key()

    await octomate.kick(UserMessageSignal([event]))

    assert len(agent.turns) == 1
    assert agent.turns[0].prompt == str(event)
    assert agent.turns[0].address == address
    assert agent.turns[0].run_name == "triage"
    assert len(reception.turns) == 1
    assert reception.turns[0].run_name == "reception"
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "handled"
    assert channel.consumed == []
    assert agent.streams == []
    assert reception.streams == []


async def test_octomate_kick_builds_prompt_from_pending_thread_messages() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="handled",
            reason="handled",
        )
    )
    channel = FakeChannelTentacle(config=_non_streaming_config())
    _register_agents(octomate, agent)
    octomate.connect(channel)

    events = [
        _event(message_id="m1", text="first detail"),
        _event(message_id="m2", user_id="bob", text="second detail"),
        _event(message_id="m3", text="wake now"),
    ]
    stored = [
        await octomate.thread_manager.record_inbound(event)
        for event in events
    ]

    await octomate.kick(
        UserMessageSignal(
            [events[-1]],
            trigger_thread_message_id=stored[-1].id,
        )
    )

    assert len(agent.turns) == 1
    assert agent.turns[0].prompt == (
        "anonymous (alice) #msg:m1:\nfirst detail\n\n"
        "anonymous (bob) #msg:m2:\nsecond detail\n\n"
        "anonymous (alice) #msg:m3:\nwake now"
    )
    assert agent.turns[0].source_thread_address == _key()
    assert agent.turns[0].source_thread_message_ids == [
        message.id for message in stored
    ]


async def test_octomate_kick_streams_reception_result_when_enabled() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="test",
            reason="debugging",
            hint="debugging",
            summon="Please continue debugging in reception.",
        )
    )
    channel = FakeChannelTentacle(config=_streaming_config())
    reception = FakeAgent(id="other")
    _register_agents(octomate, agent, reception)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event()]))

    assert len(channel.sub_threads) == 1
    assert channel.sub_threads[0][1] == "debugging"
    assert len(channel.consumed) == 1
    assert channel.consumed[0][0].thread_id == "hint-thread"
    assert channel.sent[-1][2][0]["text"] == "handled"
    assert len(agent.turns) == 1
    assert agent.streams == []
    assert len(reception.streams) == 1
    assert agent.turns[0].run_name == "triage"
    assert reception.streams[0].prompt == "Please continue debugging in reception."
    assert reception.streams[0].history == []
    assert reception.streams[0].address.thread_id == "hint-thread"
    assert reception.streams[0].run_name == "reception"


async def test_octomate_kick_skips_triage_inside_flat_thread() -> None:
    octomate = Octomate()
    agent = FakeAgent()
    reception = FakeAgent(id="other")
    channel = FakeChannelTentacle(config=_streaming_config())
    _register_agents(octomate, agent, reception)
    octomate.connect(channel)

    address = _key(thread_id="existing-thread")
    event = _event(text="continue", thread_id="existing-thread")

    await octomate.kick(UserMessageSignal([event]))

    assert agent.turns == []
    assert agent.streams == []
    assert len(reception.streams) == 1
    assert reception.streams[0].address == address
    assert reception.streams[0].run_name == "reception"
    assert channel.sub_threads == []
    assert channel.consumed[0][0] == address
    assert channel.sent[-1][2][0]["text"] == "handled"


async def test_octomate_kick_routes_summoned_reception_to_source_sub_thread() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="test",
            reason="needs work",
            hint="Working on it",
            summon="Please investigate this in ops.",
        )
    )
    source = FakeChannelTentacle(config=_streaming_config())
    ops = FakeChannelTentacle(id="ops", config=_streaming_config())
    reception = FakeAgent(id="other")
    _register_agents(octomate, agent, reception)
    octomate.connect(source)
    octomate.connect(ops)

    await octomate.kick(UserMessageSignal([_event(text="please investigate")]))

    assert len(source.sub_threads) == 1
    assert source.sub_threads[0][1] == "Working on it"
    assert len(source.consumed) == 1
    target_address = source.consumed[0][0]
    assert target_address.channel_tentacle_id == "im"
    assert target_address.chat_id == "alice"
    assert target_address.thread_id == "hint-thread"
    assert source.sent[-1][2][0]["text"] == "handled"
    assert ops.sent == []
    assert ops.sub_threads == []
    assert len(agent.turns) == 1
    assert agent.streams == []
    assert len(reception.streams) == 1
    assert agent.turns[0].run_name == "triage"
    assert reception.streams[0].prompt == "Please investigate this in ops."
    assert reception.streams[0].history == []
    assert reception.streams[0].address == target_address
    assert reception.streams[0].run_name == "reception"


async def test_octomate_kick_keeps_reception_in_main_for_main_only_channel() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="test",
            reason="needs work",
            hint="needs work",
            summon="Please investigate this in main.",
        )
    )
    channel = MainOnlyChannelTentacle(config=_streaming_config())
    reception = FakeAgent(id="other")
    _register_agents(octomate, agent, reception)
    octomate.connect(channel)

    address = _key()

    await octomate.kick(UserMessageSignal([_event(text="please investigate")]))

    assert channel.sub_threads == []
    assert len(channel.consumed) == 1
    assert channel.consumed[0][0] == address
    assert agent.turns[0].run_name == "triage"
    assert agent.streams == []
    assert reception.streams[0].prompt == "Please investigate this in main."
    assert reception.streams[0].address == address
    assert reception.streams[0].run_name == "reception"


async def test_channel_reception_model_is_resolved_from_agent() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="openai:gpt-4o-mini",
            reason="needs stronger model",
            hint="needs stronger model",
            summon="Use the stronger model.",
        ),
    )
    reception = FakeAgent(id="other", models={"openai:gpt-4o-mini": "pro-m"})
    octomate.connect(agent)
    octomate.connect(reception)
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[AgentModelConfig(agent="other", model="openai:gpt-4o-mini")],
        )
    )
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please work")]))

    assert reception.streams[0].model == "pro-m"


async def test_handoff_owner_handles_next_message_in_same_thread() -> None:
    octomate = Octomate()
    triage = FakeAgent(
        id="inkling",
        triage_output=SummonDecision(
            action="summon",
            agent_id="claude",
            model="opus",
            reason="needs code work",
            hint="Working on it",
            summon="Please debug this.",
        ),
    )
    claude = FakeAgent(id="claude")
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            triage=AgentModelConfig(agent="inkling", model="test"),
            receptions=[AgentModelConfig(agent="claude", model="opus")],
        )
    )
    _register_agents(octomate, triage, claude)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please debug")]))

    handoff_address = _key(thread_id="hint-thread")
    handoff_thread = await octomate.thread_manager.ensure_thread(handoff_address)
    conversation = await octomate.conversations.ensure(
        handoff_address,
        agent_tentacle_id="claude",
    )
    assert handoff_thread.active_agent_tentacle_id == "claude"
    assert conversation.thread_id == handoff_thread.id

    await octomate.kick(
        UserMessageSignal(
            [_event(text="more detail", thread_id="hint-thread")]
        )
    )

    assert [turn.run_name for turn in triage.turns] == ["triage"]
    assert [stream.run_name for stream in claude.streams] == [
        "reception",
        "reception",
    ]
    assert claude.streams[1].address == handoff_address
    assert claude.streams[1].thread_id == handoff_thread.id
    assert [handoff.to_agent_tentacle_id for handoff in handoff_thread.handoffs] == [
        "claude"
    ]


async def test_chained_handoff_updates_thread_owner() -> None:
    octomate = Octomate()
    triage = FakeAgent(
        id="triage",
        triage_output=SummonDecision(
            action="summon",
            agent_id="first",
            model="test",
            reason="needs first pass",
            hint="First pass",
            summon="First agent brief.",
        ),
    )
    first = FakeAgent(
        id="first",
        reception_summon=SummonDecision(
            action="summon",
            agent_id="second",
            model="test",
            reason="needs second pass",
            hint="Second pass",
            summon="Second agent brief.",
        ),
        allow_reception_run=True,
    )
    second = FakeAgent(
        id="second",
        reception_output="done",
        allow_reception_run=True,
    )
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=False),
            triage=AgentModelConfig(agent="triage", model="test"),
            receptions=[
                AgentModelConfig(agent="first", model="test"),
                AgentModelConfig(agent="second", model="test"),
            ],
        )
    )
    _register_agents(octomate, triage, first, second)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please debug")]))

    handoff_thread = await octomate.thread_manager.ensure_thread(
        _key(thread_id="hint-thread")
    )
    assert [handoff.to_agent_tentacle_id for handoff in handoff_thread.handoffs] == [
        "first",
        "second",
    ]
    assert [handoff.from_agent_tentacle_id for handoff in handoff_thread.handoffs] == [
        "triage",
        "first",
    ]
    assert handoff_thread.active_agent_tentacle_id == "second"
    assert [turn.prompt for turn in first.turns] == ["First agent brief."]
    assert [turn.prompt for turn in second.turns] == ["Second agent brief."]


async def test_direct_triage_answer_does_not_claim_thread_owner() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=DirectAnswerDecision(
            action="direct_answer",
            target_id="im",
            answer="handled",
            reason="handled",
        )
    )
    channel = FakeChannelTentacle(config=_non_streaming_config())
    _register_agents(octomate, agent)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="answer directly")]))

    thread = await octomate.thread_manager.ensure_thread(_key())
    assert list(thread.handoffs) == []
    assert thread.active_agent_tentacle_id is None


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
