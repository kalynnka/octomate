"""Prepared resources for tests that call a tentacle without the core graph."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import UUID7
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.habitat import NativeHabitat
from octomate.managers.habitat import Invocation
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import Thread
from octomate.schemas.volumes import TreeVolume
from octomate.tentacles.agent import AgentTentacle


@asynccontextmanager
async def invocation_for(
    agent: AgentTentacle,
    *,
    conversation_address: ChannelAddress,
    thread_id: UUID7,
    conversation_id: UUID7 | None = None,
    owner_id: UUID7 | None = None,
) -> AsyncIterator[Invocation]:
    conversation = await agent.resolve_conversation(
        thread_id,
        conversation_address=conversation_address,
        conversation_id=conversation_id,
    )
    if (
        owner_id is not None
        and await agent.threads.get(thread_id, with_messages=False) is None
    ):
        async with async_session() as database:
            database.add(
                Thread(
                    id=thread_id,
                    kind=conversation_address.chat_type,
                    chat_type=conversation_address.chat_type,
                    chat_id=str(thread_id),
                    channel_tentacle_id=conversation_address.channel_tentacle_id,
                )
            )
            await database.commit()
    project = await agent.run_project(thread_id)
    async with agent.workspaces.open(thread_id, project, owner_id) as workspace:
        yield Invocation(
            id=uuid7(),
            conversation_id=conversation.id,
            workspace=workspace,
            habitat=NativeHabitat(),
        )


async def volume_for(
    workspaces: WorkspaceManager, thread_id: UUID7, owner_id: UUID7
) -> TreeVolume:
    async with async_session() as session:
        thread = await session.get(Thread, thread_id)
        assert thread is not None
        project = await workspaces.projects.of(thread)
    volume = await workspaces.find(owner_id, thread_id, project.id if project else None)
    assert volume is not None
    return volume
