"""Integration tests for the Octopus message loop."""

import asyncio

import pytest

from tests.helpers import (
    MockTentacle,
    make_group_event,
    make_octopus,
    make_private_event,
    rolling_loop,
    silent_model,
    text_response_model,
)


@pytest.fixture
def octopus():
    return make_octopus()


@pytest.fixture
def tentacle(octopus):
    t = MockTentacle("test", octopus)
    octopus.connect(t)
    return t


async def test_private_message_gets_response(octopus, tentacle):
    """A private message should be processed and produce a twitch."""
    with tentacle.flick.override(model=text_response_model("pong")):
        async with rolling_loop(octopus):
            tentacle.inject(make_private_event(text="ping"))
            await asyncio.sleep(0.05)

    assert len(tentacle.sent) == 1
    _, segments = tentacle.sent[0]
    assert any(getattr(s, "data", {}).get("text") == "pong" for s in segments)


async def test_group_message_without_mention_is_silent(octopus, tentacle):
    """A group message without @mention should be silently recorded — no twitch."""
    with tentacle.flick.override(model=text_response_model("should not appear")):
        async with rolling_loop(octopus):
            tentacle.inject(make_group_event(mention_bot=False))
            await asyncio.sleep(0.05)

    assert tentacle.sent == []


async def test_group_message_with_mention_gets_response(octopus, tentacle):
    """A group message @mentioning the bot should produce a twitch."""
    with tentacle.flick.override(model=text_response_model("hi there")):
        async with rolling_loop(octopus):
            tentacle.inject(make_group_event(mention_bot=True))
            await asyncio.sleep(0.05)

    assert len(tentacle.sent) == 1


async def test_silent_agent_does_not_twitch(octopus, tentacle):
    """When the agent returns an empty message list, twitch should not be called."""
    with tentacle.flick.override(model=silent_model()):
        async with rolling_loop(octopus):
            tentacle.inject(make_private_event())
            await asyncio.sleep(0.05)

    assert tentacle.sent == []


async def test_rapid_messages_batched_into_one_call(octopus, tentacle):
    """Multiple messages from the same user sent within the flush window
    should be processed as a single batch (one agent call, one twitch)."""
    call_count = 0
    original_flick = octopus.flick

    async def counting_flick(key, batch, **kwargs):
        nonlocal call_count
        call_count += 1
        await original_flick(key, batch, **kwargs)

    octopus.flick = counting_flick

    with tentacle.flick.override(model=text_response_model("ok")):
        async with rolling_loop(octopus):
            tentacle.inject(make_private_event(text="msg1"))
            tentacle.inject(make_private_event(text="msg2"))
            tentacle.inject(make_private_event(text="msg3"))
            await asyncio.sleep(0.05)

    assert call_count == 1


async def test_messages_from_different_users_are_independent(octopus, tentacle):
    """Messages from two different users should produce two separate agent calls."""
    call_count = 0
    original_flick = octopus.flick

    async def counting_flick(key, batch, **kwargs):
        nonlocal call_count
        call_count += 1
        await original_flick(key, batch, **kwargs)

    octopus.flick = counting_flick

    with tentacle.flick.override(model=silent_model()):
        async with rolling_loop(octopus):
            tentacle.inject(make_private_event(user_id="alice"))
            tentacle.inject(make_private_event(user_id="bob"))
            await asyncio.sleep(0.05)

    assert call_count == 2


async def test_group_message_without_mention_still_recorded(octopus, tentacle):
    """Even when the bot is not @mentioned, group messages should be recorded
    in memory (not silently dropped)."""
    with tentacle.flick.override(model=silent_model()):
        async with rolling_loop(octopus):
            event = make_group_event(user_id="alice", group_id="g1", mention_bot=False)
            tentacle.inject(event)
            await asyncio.sleep(0.05)

    key = event.session_key
    history = tentacle.memory.history(key)
    assert len(history) > 0
