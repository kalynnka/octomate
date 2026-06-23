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
from octomate.triage import SummonDecision
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
        receptions=[AgentModelConfig(agent="other")],
    )


def _non_streaming_config() -> ChannelConfig:
    return ChannelConfig(
        type="fake",
        stream=ChannelStreamConfig(enabled=False),
        receptions=[AgentModelConfig(agent="other")],
    )


def _event(
    *,
    tentacle_id: str = "im",
    text: str = "hi",
    thread_id: str = "",
) -> MessageEvent:
    return MessageEvent(
        tentacle_id=tentacle_id,
        message_id="m1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
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
            model="",
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


async def test_octomate_kick_streams_reception_result_when_enabled() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=SummonDecision(
            action="summon",
            agent_id="other",
            model="",
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
            model="",
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
            model="",
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
            model="pro",
            reason="needs stronger model",
            hint="needs stronger model",
            summon="Use the stronger model.",
        ),
    )
    reception = FakeAgent(id="other", models={"pro": "pro-m"})
    octomate.connect(agent)
    octomate.connect(reception)
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[AgentModelConfig(agent="other", model="pro")],
        )
    )
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please work")]))

    assert reception.streams[0].model == "pro-m"


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
