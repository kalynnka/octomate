"""Runtime-specific requirements for forking a conversation."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.managers.conversation import ConversationManager
from octomate.schemas.events import MessageEvent
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.agent import AgentInk
from octomate.tentacles.inkling import InklingTentacle
from tests.support.users import a_user


@pytest.mark.parametrize("external_id", [None, "unexpected-runtime"])
async def test_shared_fork_can_share_history_without_an_external_session(
    in_memory_engine: AsyncEngine,
    external_id: str | None,
) -> None:
    app = Octomate()
    agent = InklingTentacle(
        "inkling",
        app,
        auth=app.auth,
        habitats=app.habitats["native"],
        models={"test": TestModel()},
    )
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

    assert isinstance(agent.ink, AgentInk)
    assert agent.supports_session_fork
    if external_id is not None:
        await app.conversations.set_external_id(source, external_id)
        with pytest.raises(ValueError, match="cannot have an external session id"):
            await agent.fork(
                source,
                ThreadKey("trunkline", "thread", "owner", "destination"),
                sender=sender,
                model="test",
            )
        return

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
    assert (await ConversationManager().history(source.id))[0].id == (
        await ConversationManager().history(target.id)
    )[0].id
    assert target_thread.active_model == "test"
