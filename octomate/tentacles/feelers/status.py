"""Transient status presentation from the existing reflex and run events."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import ClassVar

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
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.feelers.output import humanize_tool_name

logger = logging.getLogger(__name__)

type StatusUpdate = tuple[ChannelAddress | None, str | None, float]


class StatusFeeler(ABC):
    """Coalesce status writes per thread, independently of timeline rendering."""

    timeout: ClassVar[float] = 120.0  # Silence dismisses the indicator, not the run.
    refresh_interval: ClassVar[float] = 1.0  # Bound repeated-label platform calls.

    def __init__(self) -> None:
        self.queues: dict[ThreadKey, asyncio.Queue[StatusUpdate]] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    @abstractmethod
    async def present(self, address: ChannelAddress, text: str) -> None:
        """Set the platform's indicator; an empty string dismisses it."""

    def feed(self, address: ChannelAddress, event: WireEvent) -> None:
        """Queue a presentation hint without waiting for platform I/O."""
        text: str | None
        match event:
            case ReflexAwakeEvent():
                text = "Preparing…"
            case ReflexPhaseChangedEvent():
                text = {
                    "preparing": "Preparing…",
                    "routing": "Routing…",
                    "moving": "Moving…",
                    "finalizing": "Finalizing…",
                }[event.phase]
            case RunStartedEvent():
                text = "Working…"
            case RunActivityChangedEvent():
                label = "".join(
                    char
                    for char in event.display_text or ""
                    if char.isprintable() or char.isspace()
                )
                text = (
                    " ".join(label.split())
                    or {
                        "working": "Working…",
                        "thinking": "Thinking…",
                        "using_tools": "Using tools…",
                        "writing": "Writing the response…",
                        "compacting": "Compacting context…",
                    }[event.activity]
                )
            case (
                PartStartEvent(part=ThinkingPart())
                | PartDeltaEvent(delta=ThinkingPartDelta())
            ):
                text = "Thinking…"
            case (
                PartStartEvent(part=TextPart()) | PartDeltaEvent(delta=TextPartDelta())
            ):
                text = "Writing the response…"
            case PartStartEvent(part=ToolCallPart()):
                text = "Working…"
            case FunctionToolCallEvent() | OutputToolCallEvent():
                text = f"{humanize_tool_name(event.part.tool_name)}…"
            case ActionBatchEvent():
                text = (
                    "Waiting for input…"
                    if event.blocks_run is True
                    else "Working… · Input requested"
                )
            case RunEndedEvent() | ReflexAsleepEvent() | RunErrorEvent():
                text = ""
            case PartEndEvent() | FunctionToolResultEvent() | OutputToolResultEvent():
                # One tool finishing does not mean its parallel siblings finished.
                text = None
            case _:
                return
        if text:
            text = "".join(char for char in text if char.isprintable())[:120]
        key = ThreadKey.from_address(address)
        queue = self.queues.get(key)
        if queue is None:
            if not text:
                return
            queue = asyncio.Queue()
            self.queues[key] = queue
            task = asyncio.create_task(self.render(key, address, queue))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        queue.put_nowait((address, text, asyncio.get_running_loop().time()))

    async def render(
        self,
        key: ThreadKey,
        address: ChannelAddress,
        queue: asyncio.Queue[StatusUpdate],
    ) -> None:
        """One writer owns a thread's pending hints and inactivity deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        text = ""
        shown: str | None = None
        shown_at = 0.0
        try:
            while True:
                try:
                    target, update, received_at = await asyncio.wait_for(
                        queue.get(), max(0, deadline - loop.time())
                    )
                    address = target or address
                except TimeoutError:
                    update, received_at = "", loop.time()
                if update is not None:
                    text = update
                deadline = received_at + self.timeout
                while not queue.empty():
                    target, update, received_at = queue.get_nowait()
                    address = target or address
                    text = update if update is not None else text
                    deadline = received_at + self.timeout
                if loop.time() >= deadline:
                    text = ""
                if text != shown or (
                    text and loop.time() - shown_at >= self.refresh_interval
                ):
                    await self.display(address, text)
                    shown, shown_at = text, loop.time()
                if not text and queue.empty():
                    return
        finally:
            self.queues.pop(key)

    async def display(self, address: ChannelAddress, text: str) -> None:
        """A failed status hint must not interrupt content or later status delivery."""
        try:
            await self.present(address, text)
        except Exception:
            logger.warning("Status presentation failed for %s", address, exc_info=True)

    async def close(self) -> None:
        """Let in-flight writes settle, then clear each thread last."""
        for queue in self.queues.values():
            queue.put_nowait((None, "", asyncio.get_running_loop().time()))
        if self.tasks:
            pending = asyncio.gather(*self.tasks)
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise
