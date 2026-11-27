"""Authenticated command discovery and execution for browser clients."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import JsonValue

from octomate.auth import browser_request, current_user
from octomate.base import Octomate
from octomate.dependencies import (
    application,
    conversation_manager,
    thread_manager,
    workspace_manager,
)
from octomate.dependencies import (
    nerves as nervous_system,
)
from octomate.events import ReflexAsleepEvent, ReflexAwakeEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.nerve import AgentTopic, Nerves, ThreadTopic
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandError,
    CommandIntent,
    CommandOutcomeEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.schemas.user import User
from octomate.signals import CommandSignal

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
    """Resolve inspection context; reject foreign-agent conversations with HTTP 422
    before discovery, workspace lookup, or graph execution.
    """
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
            conversation = await conversations.get(conversation_id, with_runs=False)
        except ValueError as error:
            raise HTTPException(
                status_code=404, detail="Command conversation is unavailable"
            ) from error
        thread = await threads.get(conversation.thread_id, with_messages=False)
        if thread is None:
            raise HTTPException(
                status_code=404, detail="Command conversation is unavailable"
            )
        if thread.kind == "native_thread":
            raise HTTPException(409, "Synced native sessions are read-only.")
        if conversation.agent_tentacle_id != agent.id:
            raise HTTPException(
                status_code=422,
                detail="Command conversation belongs to another agent",
            )
        surface = await threads.surface(thread)
        model = surface.active_model
        permission_mode = conversation.permission_mode or agent.default_permission_mode
        project = await thread.project
        volume = await workspaces.volumes.find(
            user.id, thread.id, project.id if project is not None else None
        )
        cwd = volume.path if volume is not None else None

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
            "conversation": {
                "runs",
                "messages",
                "external_id",
                "allowed_tools",
            }
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


@command_router.post("/execute", response_model_exclude={"request_id"})
async def execute_command(
    context: Annotated[CommandContext, Depends(command_context)],
    nerves: Annotated[Nerves, Depends(nervous_system)],
    command_id: Annotated[
        str, Body(min_length=1, description="The selected descriptor's opaque ID.")
    ],
    delivery_id: Annotated[
        str,
        Body(
            min_length=1,
            description="Delivery ID. Existing conversations deduplicate matching retries; "
            "executions without a conversation have no persisted receipt.",
        ),
    ],
    arguments: Annotated[
        str, Body(description="Raw command arguments, preserved unchanged.")
    ] = "",
    attachments: Annotated[
        list[JsonValue] | None,
        Body(
            max_length=0,
            description="Must be empty until browser uploads are supported.",
        ),
    ] = None,
) -> CommandOutcomeEvent:
    """Execute explicit command intent in the resolved context.

    A descriptor may permit a direct result before a conversation exists. Such
    execution creates no thread, conversation, run or receipt; it is not replayable.

    Every execution answers with the terminal command outcome, after cleanup and
    persistence; clients must not render it as another channel message. Every
    channel executes through the Reflex command node, which publishes feedback,
    run output and approvals on the nerve: the destination channel draws them,
    and the thread's live socket carries them to a watching console, up to the
    reflex falling asleep. A failed execution answers no outcome.
    Authentication and request-validation errors remain non-2xx JSON responses.
    """
    intent = CommandIntent(command_id=command_id, arguments=arguments)
    signal = CommandSignal(context, intent, delivery_id)
    outcome: CommandOutcomeEvent | None = None
    admitted = False
    async with nerves.events.subscribe(
        ThreadTopic(ThreadKey.from_address(context.address))
    ) as events:
        nerves.signals.dispatch(AgentTopic(context.agent_id), signal)
        async for impulse in events:
            event = impulse.event
            if (
                not isinstance(
                    event, ReflexAwakeEvent | CommandOutcomeEvent | ReflexAsleepEvent
                )
                or event.request_id != signal.request_id
            ):
                continue
            if isinstance(event, ReflexAwakeEvent):
                admitted = True
            if isinstance(event, CommandOutcomeEvent):
                outcome = event.model_copy(update={"request_id": None})
            if (
                not admitted
                and isinstance(event, CommandOutcomeEvent)
                and isinstance(event.outcome, CommandError)
                and event.outcome.status == "busy"
            ):
                raise HTTPException(status_code=409, detail=event.outcome.message)
            if isinstance(event, ReflexAsleepEvent):
                break
        else:
            raise RuntimeError("The command stream ended before completion")
    if outcome is None:
        raise HTTPException(status_code=500, detail="Command execution failed")
    return outcome
