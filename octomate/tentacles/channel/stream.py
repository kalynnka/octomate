from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

StreamBlockType = Literal["answer", "thinking", "tool_call", "tool_result", "subagent"]
StreamBlockStatus = Literal["streaming", "done", "error"]


@dataclass(frozen=True)
class StreamBlock:
    id: str
    type: StreamBlockType = "answer"
    title: str = ""
    foldable: bool = False
    status: StreamBlockStatus = "streaming"


@dataclass(frozen=True)
class BatchedTextUpdate:
    block_id: str
    block_type: StreamBlockType
    title: str
    delta_text: str
    full_text: str
    sequence: int
    is_final: bool = False
    foldable: bool = False
    status: StreamBlockStatus = "streaming"


@dataclass
class TextStreamBuffer:
    block: StreamBlock
    full_text: str = ""
    pending_delta: str = ""
    sequence: int = 0
    last_flush_at: float = 0.0


class TextStreamBatcher:
    def __init__(
        self,
        *,
        flush_interval: float = 0.5,
        min_chars: int = 120,
        max_chars: int = 1000,
        fold_threshold: int = 1500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.flush_interval = max(flush_interval, 0.0)
        self.min_chars = max(min_chars, 0)
        self.max_chars = max(max_chars, 1)
        self.fold_threshold = max(fold_threshold, 1)
        self.clock = clock
        self.buffers: dict[str, TextStreamBuffer] = {}
        self.active_block_id: str | None = None

    def push_text(
        self,
        text: str,
        *,
        block: StreamBlock | None = None,
    ) -> list[BatchedTextUpdate]:
        if not text:
            return []
        block = block or StreamBlock(id="answer")
        updates: list[BatchedTextUpdate] = []
        if self.active_block_id and self.active_block_id != block.id:
            previous = self.flush_block(self.active_block_id)
            if previous is not None:
                updates.append(previous)

        self.active_block_id = block.id
        buffer = self.buffers.get(block.id)
        if buffer is None:
            buffer = TextStreamBuffer(block=block, last_flush_at=self.clock())
            self.buffers[block.id] = buffer
        else:
            buffer.block = block

        buffer.full_text += text
        buffer.pending_delta += text
        if self.should_flush(buffer):
            update = self.flush_block(block.id)
            if update is not None:
                updates.append(update)
        return updates

    def flush_block(
        self,
        block_id: str,
        *,
        is_final: bool = False,
    ) -> BatchedTextUpdate | None:
        buffer = self.buffers.get(block_id)
        if buffer is None or not buffer.pending_delta:
            return None
        buffer.sequence += 1
        update = BatchedTextUpdate(
            block_id=buffer.block.id,
            block_type=buffer.block.type,
            title=buffer.block.title,
            delta_text=buffer.pending_delta,
            full_text=buffer.full_text,
            sequence=buffer.sequence,
            is_final=is_final,
            foldable=buffer.block.foldable
            or len(buffer.full_text) >= self.fold_threshold,
            status="done" if is_final else buffer.block.status,
        )
        buffer.pending_delta = ""
        buffer.last_flush_at = self.clock()
        return update

    def finish_all(self) -> list[BatchedTextUpdate]:
        updates: list[BatchedTextUpdate] = []
        if self.active_block_id:
            update = self.flush_block(self.active_block_id, is_final=True)
            if update is not None:
                updates.append(update)
        for block_id in list(self.buffers):
            if block_id == self.active_block_id:
                continue
            update = self.flush_block(block_id, is_final=True)
            if update is not None:
                updates.append(update)
        return updates

    def full_text(self, block_id: str = "answer") -> str:
        buffer = self.buffers.get(block_id)
        return buffer.full_text if buffer is not None else ""

    def should_flush(self, buffer: TextStreamBuffer) -> bool:
        if self.flush_interval <= 0:
            return True
        if len(buffer.pending_delta) >= self.max_chars:
            return True
        if len(buffer.pending_delta) < self.min_chars:
            return False
        return self.clock() - buffer.last_flush_at >= self.flush_interval


def render_text_stream_delta(
    event: AgentStreamEvent | AgentRunResultEvent[Any],
) -> str:
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return ""
