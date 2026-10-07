"""Runtime-specific requirements for forking a conversation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.inkling import InklingTentacle
from tests.support.users import a_user


async def test_inkling_fork_needs_no_external_session(tmp_path: Path) -> None:
    tentacle = InklingTentacle("inkling", Octomate(), models={"test": TestModel()})
    conversation = Conversation(thread_id=uuid7(), agent_tentacle_id="inkling")

    assert await tentacle.fork_session(conversation, cwd=tmp_path) is None

    conversation.external_id = "unexpected-runtime"
    with pytest.raises(ValueError, match="cannot have an external session id"):
        await tentacle.fork_session(conversation, cwd=tmp_path)


async def test_shared_fork_can_copy_history_without_an_external_session(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    agent = InklingTentacle("inkling", app, models={"test": TestModel()})
    owner = await a_user()
    sender = UserProfile(channel_user_id="owner", user_id=owner.id)
    key = ThreadKey("trunkline", "thread", "owner", "source")
    source_thread = await app.threads.ensure(key)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=key.channel_tentacle_id,
            chat_type=key.chat_type,
            chat_id=key.chat_id,
            channel_thread_id=key.channel_thread_id,
            user_id=sender.channel_user_id,
            sender=sender,
        )
    )
    source = await app.conversations.ensure(
        source_thread.id, agent_tentacle_id=agent.id
    )
    await app.conversations.record_agent_run(
        source,
        run_id=str(uuid7()),
        messages=[ModelResponse(parts=[TextPart("Original answer")])],
    )

    target_thread = await agent.fork(
        source,
        ThreadKey("trunkline", "thread", "owner", "destination"),
        sender=sender,
        model="test",
    )

    source = await app.conversations.get(source.id)
    target = await app.conversations.ensure(
        target_thread.id, agent_tentacle_id=agent.id
    )
    assert source.external_id is target.external_id is None
    assert source.messages[0].parts == target.messages[0].parts
    assert source.messages[0].id != target.messages[0].id
    assert target_thread.active_model == "test"
