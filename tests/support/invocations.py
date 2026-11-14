"""Prepared resources for tests that call a tentacle without the core graph."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from pydantic import UUID7

from octomate.database import async_session
from octomate.managers.habitat import Invocation
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import GitVolume
from octomate.tentacles.agent import AgentTentacle


@asynccontextmanager
async def invocation_for(
    agent: AgentTentacle,
    *,
    conversation_address: ChannelAddress,
    thread_id: UUID7,
    conversation_id: UUID7 | None = None,
    conversation: Conversation | None = None,
    owner_id: UUID7 | None = None,
) -> AsyncGenerator[Invocation]:
    if conversation is None:
        conversation = await agent.resolve_conversation(
            thread_id,
            conversation_address=conversation_address,
            conversation_id=conversation_id,
        )
    thread = (
        await agent.threads.get(thread_id, with_messages=False)
        if owner_id is not None or agent.workspaces.projects.roots
        else None
    )
    if thread is None:
        thread = Thread(
            id=thread_id,
            kind=conversation_address.chat_type,
            chat_type=conversation_address.chat_type,
            chat_id=str(thread_id),
            channel_tentacle_id=conversation_address.channel_tentacle_id,
        )
        if owner_id is not None:
            async with async_session() as database:
                database.add(thread)
                await database.commit()
    async with agent.habitats.invoke(
        thread=thread,
        conversation=conversation,
        owner_id=owner_id,
    ) as invocation:
        yield invocation


async def volume_for(
    workspaces: WorkspaceManager, thread_id: UUID7, owner_id: UUID7
) -> GitVolume:
    async with async_session() as session:
        thread = await session.get(Thread, thread_id)
        assert thread is not None
        project = await workspaces.projects.of(thread)
    volume = await workspaces.volumes.find(
        owner_id, thread_id, project.id if project else None
    )
    assert volume is not None
    return volume
