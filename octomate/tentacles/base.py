from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

import anyio

from octomate.config import TentacleConfig
from octomate.schemas.events import GroupMessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.abc import TaskGroup

    from octomate.nerve import OctopusNerve
    from octomate.schemas.adaptors import ActionUnion
    from octomate.schemas.events import MessageEvent
    from octomate.schemas.segments import AgentSegment

logger = logging.getLogger(__name__)


@dataclass
class SendTarget:
    chat_type: Literal["group", "private"]
    chat_id: int | str
    reply_to: int | str | None = None


class BaseTentacle(ABC):
    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    nerve: OctopusNerve
    config: TentacleConfig
    name: str
    self_id: int | str | None
    self_name: str | None
    _buffer: MessageBuffer

    def __init__(self, config: TentacleConfig, flush_delay: float = 0.5) -> None:
        self.config = config
        self.name = config.name
        self.self_id: int | str | None = None
        self.self_name: str | None = None
        self._buffer = MessageBuffer(
            flush_delay=flush_delay,
            handler=self._deliver_batch,
        )

    async def _deliver_batch(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self.nerve.kick(key, batch)

    def sense(self, event: MessageEvent) -> None:
        self._buffer.push(event)

    def save_dir(self, event: MessageEvent) -> Path:
        if isinstance(event, GroupMessageEvent):
            subdir = f"g{event.group_id}"
        else:
            subdir = f"u{event.user_id}"
        return self.FILES_ROOT / self.name / subdir

    async def prepare_inbound_message(self, event: MessageEvent) -> None:
        pending = [seg for seg in event.message if isinstance(seg, ImageSegment)]
        if not pending:
            return
        save = self.save_dir(event)
        await anyio.Path(save).mkdir(parents=True, exist_ok=True)
        async with anyio.create_task_group() as tg:
            for seg in pending:
                tg.start_soon(self.download_image, seg, save)

    @abstractmethod
    async def download_image(self, seg: ImageSegment, save_dir: Path) -> None: ...

    async def prepare_outbound_message(self, segments: list[AgentSegment]) -> None:
        for seg in segments:
            if isinstance(seg, ImageSegment):
                await self.prepare_image(seg)

    @abstractmethod
    async def prepare_image(self, seg: ImageSegment) -> None: ...

    async def act(self, action: ActionUnion) -> None:
        from octomate.schemas.actions import SendGroupMsgAction, SendPrivateMsgAction

        if isinstance(action, SendGroupMsgAction):
            target = SendTarget("group", action.params.group_id, action.params.reply)
            segments = action.params.message
        elif isinstance(action, SendPrivateMsgAction):
            target = SendTarget("private", action.params.user_id, action.params.reply)
            segments = action.params.message
        else:
            await self.handle_action(action)
            return
        await self.prepare_outbound_message(segments)
        await self.send_message(target, segments)

    @abstractmethod
    async def send_message(
        self, target: SendTarget, segments: list[AgentSegment]
    ) -> None: ...

    async def handle_action(self, action: ActionUnion) -> None:
        logger.debug(
            "Tentacle %s: unhandled action type %s",
            self.name,
            type(action).__name__,
        )

    @abstractmethod
    async def introspect(self) -> None: ...

    @abstractmethod
    async def activate(self) -> None: ...

    @abstractmethod
    async def deactivate(self) -> None: ...


class MessageBuffer:
    _flush_delay: float
    _handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]]
    _buckets: defaultdict[SessionKey, list[MessageEvent]]
    _pending: set[SessionKey]
    _tg: TaskGroup | None

    def __init__(
        self,
        flush_delay: float,
        handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]],
    ) -> None:
        self._flush_delay = flush_delay
        self._handler = handler
        self._buckets: defaultdict[SessionKey, list[MessageEvent]] = defaultdict(list)
        self._pending: set[SessionKey] = set()
        self._tg: TaskGroup | None = None

    def bind(self, tg: TaskGroup) -> None:
        self._tg = tg

    def push(self, event: MessageEvent) -> None:
        key = event.session_key
        self._buckets[key].append(event)
        if key not in self._pending:
            self._pending.add(key)
            if self._tg is None:
                raise RuntimeError("MessageBuffer.bind() must be called before push()")
            self._tg.start_soon(self._flush_after_delay, key)

    async def _flush_after_delay(self, key: SessionKey) -> None:
        await anyio.sleep(self._flush_delay)
        self._pending.discard(key)
        batch = self._buckets.pop(key, [])
        if not batch:
            return
        try:
            await self._handler(key, batch)
        except Exception:
            logger.exception("Error handling batch for %s", key)
