"""Multiplex authorized thread activity without sending their message content."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING, Annotated

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import UUID7, BaseModel, Field, ValidationError
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
)
from starlette.websockets import WebSocketState

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    ReflexAsleepEvent,
    ReflexAwakeEvent,
    ReflexPhaseChangedEvent,
    RunActivityChangedEvent,
    RunEndedEvent,
    RunErrorEvent,
    RunStartedEvent,
    WireEvent,
)
from octomate.managers.thread import ThreadManager
from octomate.nerve import ThreadTopic
from octomate.schemas.thread import ThreadKey

if TYPE_CHECKING:
    from octomate.base import Octomate


class ActivityWatch(BaseModel):
    """Replace this socket's watched set; keys belong to its signed-in user."""

    thread_ids: list[UUID7] = Field(default_factory=list)
    thread_keys: list[str] = Field(default_factory=list)


class ActivityUpdate(BaseModel):
    """A bounded status projection, never thoughts, arguments, results or prompts."""

    key: str = Field(description="The client's id:UUID or key:thread-key identity.")
    event: Annotated[
        RunActivityChangedEvent | ReflexPhaseChangedEvent | RunEndedEvent,
        Field(discriminator="event_kind"),
    ]
    attention: bool = False

    @classmethod
    def from_event(cls, key: str, event: WireEvent) -> ActivityUpdate | None:
        match event:
            case ReflexAwakeEvent():
                status = ReflexPhaseChangedEvent(phase="preparing")
            case ReflexPhaseChangedEvent():
                status = event
            case RunStartedEvent() | PartStartEvent(part=ToolCallPart()):
                status = RunActivityChangedEvent(activity="working")
            case RunActivityChangedEvent():
                label = " ".join(
                    "".join(
                        char
                        for char in event.display_text or ""
                        if char.isprintable() or char.isspace()
                    ).split()
                )[:120]
                status = RunActivityChangedEvent(
                    activity=event.activity, display_text=label or None
                )
            case (
                PartStartEvent(part=ThinkingPart())
                | PartDeltaEvent(delta=ThinkingPartDelta())
            ):
                status = RunActivityChangedEvent(activity="thinking")
            case (
                PartStartEvent(part=TextPart()) | PartDeltaEvent(delta=TextPartDelta())
            ):
                status = RunActivityChangedEvent(activity="writing")
            case FunctionToolCallEvent() | OutputToolCallEvent():
                status = RunActivityChangedEvent(activity="using_tools")
            case ActionBatchEvent():
                status = RunActivityChangedEvent(
                    activity="working",
                    display_text="Waiting for input"
                    if event.blocks_run is True
                    else "Working · Input requested",
                )
            case RunEndedEvent() | ReflexAsleepEvent() | RunErrorEvent():
                status = RunEndedEvent()
            case _:
                return None
        return cls(key=key, event=status, attention=isinstance(event, ActionBatchEvent))


async def watch_activity(
    websocket: WebSocket,
    octomate: Octomate,
    threads: ThreadManager,
    user_id: UUID7,
    channel_id: str,
) -> None:
    """Keep one socket while its authorized subscriptions change in place.

    Repeated activity is sent at most every 30 seconds to refresh the client's
    inactivity deadline. Changed states and terminal events are sent immediately.
    Each authorized watch starts with current runtime activity, then new events.
    Each reader is cancelled on removal or disconnect; no state outlives the socket.
    """
    watching: dict[str, asyncio.Task[None]] = {}
    sending = asyncio.Lock()

    async def send_activity(
        identity: str, key: ThreadKey, ready: asyncio.Event
    ) -> None:
        with octomate.nerve.subscribe(ThreadTopic(key), backlog=4096) as impulses:
            task = octomate.awakes.get(key)
            agent = octomate.agents.get(key.channel_tentacle_id)
            running = (task is not None and not task.done()) or (
                agent is not None and key in agent.native_running_threads
            )
            previous = ActivityUpdate(
                key=identity,
                event=RunActivityChangedEvent(activity="working")
                if running
                else RunEndedEvent(),
            )
            async with sending:
                await websocket.send_text(previous.model_dump_json(exclude_none=True))
            sent_at = monotonic()
            ready.set()
            async for impulse in impulses:
                update = ActivityUpdate.from_event(identity, impulse.event)
                heartbeat = isinstance(
                    impulse.event,
                    (PartEndEvent, FunctionToolResultEvent, OutputToolResultEvent),
                )
                if update is None and heartbeat:
                    update = previous
                if update is None:
                    continue
                if update == previous and monotonic() - sent_at < 30:
                    continue
                async with sending:
                    await websocket.send_text(update.model_dump_json(exclude_none=True))
                previous, sent_at = update, monotonic()
            await websocket.close(code=1013, reason="activity reader fell behind")

    async def replace_watches(request: ActivityWatch, tasks: asyncio.TaskGroup) -> None:
        wanted = {f"id:{id}" for id in request.thread_ids} | {
            f"key:{key}" for key in request.thread_keys
        }
        additions: dict[str, ThreadKey] = {}
        for thread_id in request.thread_ids:
            identity = f"id:{thread_id}"
            if identity in watching:
                continue
            thread = await threads.get(thread_id, with_messages=False, user_id=user_id)
            if thread is None:
                await websocket.close(code=1008, reason="no accessible thread")
                return
            additions[identity] = thread.key
        for key in request.thread_keys:
            if f"key:{key}" not in watching:
                additions[f"key:{key}"] = ThreadKey(
                    channel_id, "thread", str(user_id), key or None
                )
        removed = [watching.pop(key) for key in list(watching) if key not in wanted]
        for task in removed:
            task.cancel()
        await asyncio.gather(*removed, return_exceptions=True)
        for identity, key in additions.items():
            ready = asyncio.Event()
            watching[identity] = tasks.create_task(send_activity(identity, key, ready))
            await ready.wait()

    await websocket.accept()
    try:
        async with asyncio.TaskGroup() as tasks:
            try:
                while True:
                    request = ActivityWatch.model_validate_json(
                        await websocket.receive_text()
                    )
                    await replace_watches(request, tasks)
                    if websocket.application_state == WebSocketState.DISCONNECTED:
                        break
            except ValidationError:
                await websocket.close(code=1008, reason="invalid activity subscription")
            except WebSocketDisconnect:
                pass
            finally:
                for task in watching.values():
                    task.cancel()
    except* (WebSocketDisconnect, OSError):
        pass
