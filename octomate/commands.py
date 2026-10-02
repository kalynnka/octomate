"""Authenticated command discovery and execution for browser clients."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, aclosing, suppress
from typing import Annotated

import anyio
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from pydantic import JsonValue
from starlette.types import Receive, Scope, Send

from octomate.auth import browser_request, current_user
from octomate.base import Octomate
from octomate.capabilities.harness.events import (
    CommandOutcomeEvent,
    CommandStreamEvent,
    wire_event_adapter,
)
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.dependencies import (
    application,
    conversation_manager,
    thread_manager,
    workspace_manager,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.reflex.state import ReflexGraphResult
from octomate.schemas.awakes import CommandSignal
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import User
from octomate.tentacles.channel import ChannelOutput
from octomate.tentacles.trunkline.base import (
    SSE_HEADERS,
    TrunklineStreamItem,
    TrunklineTentacle,
    current_sink,
    to_wire,
)

command_router = APIRouter(
    prefix="/api/commands",
    tags=["commands"],
    dependencies=[Depends(browser_request)],
)


class CommandResponse(Response):
    """Serialize command events to SSE and close their producer in the same task.

    A disconnect cancels that task once and waits for the event generator's
    cleanup. The producer owns validation, execution and outcome recording.
    Each SSE data field contains a ``CommandStreamEvent`` JSON payload. Trunkline
    presents command feedback through channel events. The terminal outcome reports
    completion after runtime cleanup and persistence succeed; it is not another
    display message.
    """

    events: AsyncGenerator[TrunklineStreamItem | CommandOutcomeEvent, None]

    def __init__(
        self,
        events: AsyncGenerator[TrunklineStreamItem | CommandOutcomeEvent, None],
    ) -> None:
        super().__init__(media_type="text/event-stream", headers=SSE_HEADERS)
        del self.headers["content-length"]
        self.events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with asyncio.TaskGroup() as tasks:
            execution = tasks.create_task(self.respond(send))
            disconnected = tasks.create_task(self.listen_for_disconnect(receive))
            await asyncio.wait(
                (execution, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if not execution.done():
                execution.cancel()
            disconnected.cancel()

    async def listen_for_disconnect(self, receive: Receive) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    async def respond(self, send: Send) -> None:
        async with aclosing(self.events) as events:
            first = await anext(events)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": self.raw_headers,
                }
            )
            await self.send_event(first, send)
            async for event in events:
                await self.send_event(event, send)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send_event(
        self,
        event: TrunklineStreamItem | CommandOutcomeEvent,
        send: Send,
    ) -> None:
        if isinstance(event, CommandOutcomeEvent):
            payload = event.model_dump_json().encode()
        else:
            wire = to_wire(event)
            if wire is None:
                return
            payload = wire_event_adapter.dump_json(wire, warnings=False)
        await send(
            {
                "type": "http.response.body",
                "body": b"data: " + payload + b"\n\n",
                "more_body": True,
            }
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


@command_router.post(
    "/execute",
    response_class=Response,
    response_model=CommandStreamEvent,
    response_description="SSE events with JSON data identified by event_kind.",
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "itemSchema": {
                        "type": "object",
                        "required": ["data"],
                        "properties": {
                            "data": {
                                "type": "string",
                                "contentMediaType": "application/json",
                                "contentSchema": {
                                    "$ref": "#/components/schemas/CommandStreamEvent"
                                },
                            }
                        },
                    }
                }
            }
        }
    },
)
async def execute_command(
    context: Annotated[CommandContext, Depends(command_context)],
    app: Annotated[Octomate, Depends(application)],
    command_id: Annotated[
        str, Body(min_length=1, description="The selected descriptor's opaque ID.")
    ],
    delivery_id: Annotated[
        str, Body(min_length=1, description="Stable delivery ID; reuse for retries.")
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
) -> CommandResponse:
    """Execute explicit command intent against an existing conversation.

    Every execution response is SSE. Each data field contains CommandStreamEvent
    JSON, identified by event_kind. Trunkline presents direct results and refusals
    through channel events. The terminal command_outcome reports completion after
    cleanup and persistence; clients must not render it as another channel message.
    A failed stream closes without an outcome.
    Authentication and request-validation errors remain non-2xx JSON responses.
    """
    agent = app.agents[context.agent_id]
    invocation = CommandInvocation(command_id=command_id, arguments=arguments)

    async def reflex_events() -> AsyncGenerator[
        TrunklineStreamItem | CommandOutcomeEvent, None
    ]:
        send, receive = anyio.create_memory_object_stream[TrunklineStreamItem](128)

        async def run() -> ReflexGraphResult | None:
            token = current_sink.set(send)
            try:
                async with send:
                    return await app.kick(
                        CommandSignal(context, invocation, delivery_id)
                    )
            finally:
                current_sink.reset(token)

        task = asyncio.create_task(run())
        try:
            async with receive:
                async for event in receive:
                    yield event
            result = await task
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        outcome = (
            result
            if isinstance(result, CommandResult | CommandError)
            else CommandResult()
        )
        yield CommandOutcomeEvent(outcome=outcome)

    if isinstance(app.channels[context.address.channel_tentacle_id], TrunklineTentacle):
        return CommandResponse(reflex_events())

    async def events() -> AsyncGenerator[
        ReactStreamEvent[ChannelOutput] | CommandOutcomeEvent, None
    ]:
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                agent.commands.validate(
                    agent, context, invocation, delivery_id=delivery_id
                )
            )
            if isinstance(validated, CommandResult | CommandError):
                result = validated
            else:
                result = await stack.enter_async_context(
                    agent.commands.execute(
                        agent, context, invocation, validated, delivery_id=delivery_id
                    )
                )
            if isinstance(result, CommandResult | CommandError):
                outcome = result
            else:
                async for event in result:
                    yield event
                outcome = CommandResult()
        yield CommandOutcomeEvent(outcome=outcome)

    return CommandResponse(events())
