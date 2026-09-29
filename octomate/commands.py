"""Authenticated command discovery shared by browser clients and IM channels."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from octomate.auth import browser_request, current_user
from octomate.base import Octomate
from octomate.dependencies import (
    application,
    conversation_manager,
    thread_manager,
    workspace_manager,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.commands import CommandCatalog, CommandContext
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import User

command_router = APIRouter(
    prefix="/api/commands",
    tags=["commands"],
    dependencies=[Depends(browser_request)],
)


async def command_context(
    agent_id: Annotated[str, Body(min_length=1, description="The selected agent.")],
    address: Annotated[
        ChannelAddress,
        Body(description="The originating channel surface."),
    ],
    user: Annotated[User, Depends(current_user)],
    app: Annotated[Octomate, Depends(application)],
    conversations: Annotated[ConversationManager, Depends(conversation_manager)],
    threads: Annotated[ThreadManager, Depends(thread_manager)],
    workspaces: Annotated[WorkspaceManager, Depends(workspace_manager)],
    conversation_id: Annotated[
        uuid.UUID | None,
        Body(description="An existing conversation; omit for a new composer."),
    ] = None,
    model: Annotated[
        str | None,
        Body(
            description="A model selection for a new composer; existing conversations use their stored model."
        ),
    ] = None,
    permission_mode: Annotated[
        str | None,
        Body(
            description="An approval posture for a new composer; existing conversations use their stored permissions."
        ),
    ] = None,
) -> CommandContext:
    """Check that the channel enables the agent, then resolve inspection context."""
    channel = app.channels.get(address.channel_tentacle_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Command channel is unavailable")
    agent = app.agents.get(agent_id)
    if agent is None or agent_id not in channel.agent_ids:
        raise HTTPException(
            status_code=404, detail="Command agent is unavailable on this channel"
        )

    conversation = None
    cwd = None
    if conversation_id is not None:
        try:
            conversation = await conversations.get(conversation_id, with_history=False)
        except ValueError as error:
            raise HTTPException(
                status_code=404, detail="Command conversation is unavailable"
            ) from error
        thread = await threads.get(conversation.thread_id, with_messages=False)
        if thread is None:
            raise HTTPException(
                status_code=404, detail="Command conversation is unavailable"
            )
        surface = await threads.surface(thread)
        model = surface.active_model
        permission_mode = conversation.permission_mode or agent.default_permission_mode
        cwd = workspaces.open(thread.id, await thread.project).path

    try:
        model = agent.resolve_model(model)
        if permission_mode is not None:
            agent.check_permission_mode(permission_mode)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return CommandContext(
        agent_id=agent_id,
        user_id=user.id,
        address=address,
        cwd=cwd,
        conversation=conversation,
        model=model,
        permission_mode=permission_mode or agent.default_permission_mode,
    )


@command_router.post(
    "/catalog",
    response_model_exclude={
        "context": {
            "conversation": {"runs", "messages", "external_id", "allowed_tools"}
        }
    },
)
async def discover_commands(
    context: Annotated[CommandContext, Depends(command_context)],
    app: Annotated[Octomate, Depends(application)],
    refresh: Annotated[
        bool, Body(description="Rediscover rather than reuse a completed catalog.")
    ] = False,
    prefix: Annotated[
        str,
        Body(
            description="Case-insensitive command-name prefix; omitted means the full catalog."
        ),
    ] = "",
) -> CommandCatalog:
    """Discover or complete commands without creating a conversation or model turn."""
    agent = app.agents[context.agent_id]
    return await agent.discover_commands(context, refresh=refresh, prefix=prefix)
