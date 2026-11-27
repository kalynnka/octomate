"""Awake signals retain their shape through the nerve's serialization boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from uuid_utils.compat import uuid7

from octomate.nerve import InvocationTopic, Nerves
from octomate.schemas.commands import CommandContext, CommandIntent
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.triage import SchemeDecision, SummonDecision, TeleportDecision
from octomate.schemas.user import UserProfile
from octomate.signals import (
    AsleepSignal,
    AwakeSignal,
    BaseSignal,
    CommandSignal,
    DeferredActionBatchResponse,
    DrivenGatewaySignal,
    NativeGatewaySignal,
    Signal,
    SummonSignal,
    UserMessageSignal,
    signal_adapter,
)
from tests.support.nerve import held

ADDRESS = ChannelAddress("web", "dm", "user", "user")


@pytest.fixture(
    params=[
        "message",
        "command",
        "new_command",
        "deferred",
        "native",
        "driven",
        "summon",
    ]
)
def awake(request: pytest.FixtureRequest) -> Signal:
    profile = UserProfile(
        channel_tentacle_id="web", channel_user_id="user", name="Caller"
    )
    decision = SummonDecision(
        agent_id="agent",
        model=None,
        reason="Selected",
        hint="Starting",
        brief="The task",
    )
    match request.param:
        case "message":
            return UserMessageSignal(
                messages=[
                    MessageEvent(
                        tentacle_id="web",
                        chat_id="user",
                        user_id="user",
                        sender=profile,
                    )
                ],
                trigger_thread_message_id=uuid7(),
                agent_id="agent",
            )
        case "command" | "new_command":
            return CommandSignal(
                context=CommandContext(
                    agent_id="agent",
                    user_id=uuid7(),
                    address=ADDRESS,
                    cwd=Path("/habitat/workspace"),
                    conversation=Conversation(
                        thread_id=uuid7(), agent_tentacle_id="agent"
                    )
                    if request.param == "command"
                    else None,
                    model="selected-model",
                    permission_mode="plan",
                ),
                intent=CommandIntent(command_id="compact", arguments="keep this"),
                delivery_id="delivery",
            )
        case "deferred":
            return DeferredActionBatchResponse(
                batch_id=uuid7(),
                answers={uuid7(): ["one", "two"]},
                approvals={uuid7(): True},
                allow_session=True,
            )
        case "native":
            return NativeGatewaySignal(
                decision=SchemeDecision(
                    hint="Continue privately", brief="The task", destination=ADDRESS
                ),
                agent_id="agent",
                user_profile=None,
                source=ADDRESS,
            )
        case "driven":
            return DrivenGatewaySignal(
                thread_id=uuid7(),
                decision=TeleportDecision(
                    agent_id="agent", hint="Move", destination=ADDRESS
                ),
                agent_id="agent",
                user_profile=profile,
                source=ADDRESS,
            )
        case "summon":
            return SummonSignal(
                decision=decision,
                destination=ADDRESS,
                agent_id="requester",
                user_profile=profile,
                source_conversation_id=uuid7(),
                project="project",
                ref="main",
            )
        case _:
            raise ValueError(f"Unknown test variant: {request.param}")


def test_existing_awakes_round_trip_without_translation(awake: Signal) -> None:
    assert isinstance(awake, AwakeSignal)
    assert isinstance(awake, BaseSignal)
    encoded = signal_adapter.dump_json(awake)
    received = signal_adapter.validate_json(encoded)
    assert type(received) is type(awake)
    assert signal_adapter.dump_json(received) == encoded
    if isinstance(received, UserMessageSignal):
        assert received.address == ADDRESS
        assert received
    if isinstance(received, CommandSignal):
        assert received.context.cwd == Path("/habitat/workspace")
        assert received.intent.arguments == "keep this"


async def test_awakes_use_the_same_signal_nerve(awake: Signal) -> None:
    nerves = Nerves()
    topic = InvocationTopic(uuid7())
    async with (
        nerves.signals.subscribe(topic) as incoming,
        nerves.events.subscribe(topic) as events,
    ):
        nerves.signals.dispatch(topic, awake)
        assert await incoming.receive() is awake
        assert held(events) == []
    assert not nerves.signals.subscribers


def test_interrupt_remains_a_control_not_a_second_run_request() -> None:
    signal = AsleepSignal(invocation_id=uuid7())
    assert isinstance(signal, BaseSignal)
    assert not isinstance(signal, AwakeSignal)
    assert signal_adapter.validate_json(signal_adapter.dump_json(signal)) == signal
    with pytest.raises(ValidationError, match="exactly one invocation or thread"):
        signal_adapter.validate_python({"event_kind": "asleep_signal"})


@pytest.mark.parametrize(
    "kind",
    [
        "run_agent_signal",
        "deferred_response_signal",
        "message_reflex_signal",
        "command_reflex_signal",
        "resume_reflex_signal",
        "native_gateway_reflex_signal",
        "driven_gateway_reflex_signal",
        "summon_reflex_signal",
    ],
)
def test_superseded_signal_tags_are_not_accepted(kind: str) -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        signal_adapter.validate_python({"event_kind": kind})


def test_message_dispatch_rejects_invented_execution_fields() -> None:
    with pytest.raises(ValidationError, match="Unexpected keyword argument"):
        signal_adapter.validate_python(
            {"event_kind": "user_message_signal", "messages": [], "priming": {}}
        )
