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
from octomate.triage import HandoffDecision
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


def _register_agents(octomate: Octomate, agent: FakeAgent) -> None:
    octomate.connect(agent)


async def test_octomate_kick_dispatches_directly_to_registered_agent() -> None:
    octomate = Octomate()
    agent = FakeAgent()
    channel = FakeChannelTentacle()
    _register_agents(octomate, agent)
    octomate.connect(channel)

    event = _event()
    address = _key()

    await octomate.kick(UserMessageSignal([event]))

    assert len(agent.turns) == 1
    assert agent.turns[0].prompt == str(event)
    assert agent.turns[0].address == address
    assert agent.turns[0].run_name == "triage"
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "handled"
    assert channel.consumed == []
    assert agent.streams == []


async def test_octomate_kick_streams_reception_result_when_enabled() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=HandoffDecision(
            action="handoff",
            target_id="im",
            agent_id="",
            model="",
            reason="debugging",
            hint="debugging",
            handoff="Please continue debugging in reception.",
        )
    )
    channel = FakeChannelTentacle(config=_streaming_config())
    _register_agents(octomate, agent)
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event()]))

    assert len(channel.sub_threads) == 1
    assert channel.sub_threads[0][1] == "debugging"
    assert len(channel.consumed) == 1
    assert channel.consumed[0][0].thread_id == "hint-thread"
    assert channel.sent[-1][2][0]["text"] == "handled"
    assert len(agent.turns) == 1
    assert len(agent.streams) == 1
    assert agent.turns[0].run_name == "triage"
    assert agent.streams[0].prompt == "Please continue debugging in reception."
    assert agent.streams[0].history == []
    assert agent.streams[0].address.thread_id == "hint-thread"
    assert agent.streams[0].run_name == "reception"


async def test_octomate_kick_skips_triage_inside_flat_thread() -> None:
    octomate = Octomate()
    agent = FakeAgent()
    channel = FakeChannelTentacle(config=_streaming_config())
    _register_agents(octomate, agent)
    octomate.connect(channel)

    address = _key(thread_id="existing-thread")
    event = _event(text="continue", thread_id="existing-thread")

    await octomate.kick(UserMessageSignal([event]))

    assert agent.turns == []
    assert len(agent.streams) == 1
    assert agent.streams[0].address == address
    assert agent.streams[0].run_name == "reception"
    assert channel.sub_threads == []
    assert channel.consumed[0][0] == address
    assert channel.sent[-1][2][0]["text"] == "handled"


async def test_octomate_kick_routes_reception_to_attached_channel_sub_thread() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=HandoffDecision(
            action="handoff",
            target_id="ops",
            agent_id="",
            model="",
            reason="needs work",
            hint="Working on it",
            handoff="Please investigate this in ops.",
        )
    )
    source = FakeChannelTentacle()
    ops = FakeChannelTentacle(id="ops", config=_streaming_config())
    _register_agents(octomate, agent)
    octomate.connect(source)
    octomate.connect(ops)

    await octomate.kick(UserMessageSignal([_event(text="please investigate")]))

    assert source.sent == []
    assert len(ops.sub_threads) == 1
    assert ops.sub_threads[0][1] == "Working on it"
    assert len(ops.consumed) == 1
    target_address = ops.consumed[0][0]
    assert target_address.channel_tentacle_id == "ops"
    assert target_address.chat_id == "alice"
    assert target_address.thread_id == "hint-thread"
    assert ops.sent[-1][2][0]["text"] == "handled"
    assert len(agent.turns) == 1
    assert len(agent.streams) == 1
    assert agent.turns[0].run_name == "triage"
    assert agent.streams[0].prompt == "Please investigate this in ops."
    assert agent.streams[0].history == []
    assert agent.streams[0].address == target_address
    assert agent.streams[0].run_name == "reception"


async def test_octomate_kick_keeps_reception_in_main_for_main_only_channel() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=HandoffDecision(
            action="handoff",
            target_id="im",
            agent_id="",
            model="",
            reason="needs work",
            hint="needs work",
            handoff="Please investigate this in main.",
        )
    )
    channel = MainOnlyChannelTentacle(config=_streaming_config())
    _register_agents(octomate, agent)
    octomate.connect(channel)

    address = _key()

    await octomate.kick(UserMessageSignal([_event(text="please investigate")]))

    assert channel.sub_threads == []
    assert len(channel.consumed) == 1
    assert channel.consumed[0][0] == address
    assert agent.turns[0].run_name == "triage"
    assert agent.streams[0].prompt == "Please investigate this in main."
    assert agent.streams[0].address == address
    assert agent.streams[0].run_name == "reception"


async def test_channel_reception_model_is_resolved_from_agent() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_output=HandoffDecision(
            action="handoff",
            target_id="im",
            agent_id="inkling",
            model="pro",
            reason="needs stronger model",
            hint="needs stronger model",
            handoff="Use the stronger model.",
        ),
        models={"pro": "pro-m"},
    )
    octomate.connect(agent)
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
            receptions=[AgentModelConfig(agent="inkling", model="pro")],
        )
    )
    octomate.connect(channel)

    await octomate.kick(UserMessageSignal([_event(text="please work")]))

    assert agent.streams[0].model == "pro-m"


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
