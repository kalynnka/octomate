from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from octomate.nerve import StreamSink
from octomate.schemas.conversation import Conversation
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.dev_ui.adapter import DevUIStreamSink
from octomate.tentacles.channel.dev_ui.chromo import StubChromo
from octomate.tentacles.channel.dev_ui.ink import StubInk

if TYPE_CHECKING:
    from fastapi import APIRouter

logger = logging.getLogger(__name__)


class DevUITentacle(ChannelTentacle):
    """HTTP-only channel: serves pydantic-ai's chat UI + a Vercel-AI SSE endpoint.

    HTTP-only means `activate()` stays a no-op (inherited from base) and
    `router()` returns the DevUI routes. The chat endpoint pre-registers a
    `DevUIStreamSink` in `octopus.sinks` before kicking — that way the
    sink exists when the agent's first `StreamFrame` arrives.
    """

    agent_id: str

    def __init__(self, id: str, *, agent_id: str = "inkling") -> None:
        self.ink = StubInk()
        self.chromo = StubChromo()
        super().__init__(id)
        self.agent_id = agent_id

    def router(self) -> APIRouter:
        from octomate.tentacles.channel.dev_ui.routes import build_dev_ui_router

        return build_dev_ui_router(self)

    async def twitch(self, key, message) -> None:
        """Discrete reply — writes a final-segments block onto the sink.

        For DevUI both `StreamFrame`s and `SendSegments` flow through the
        same per-request sink (the only "send" channel a request has is
        its own SSE response). We look up the active sink for this
        conversation and write the segments as Vercel text chunks.
        """
        sink = self.octopus.sinks.get(key)
        if isinstance(sink, DevUIStreamSink):
            from octomate.nerve import SendSegments

            await sink.write_segments(SendSegments(target_key=key, segments=message.segments))
        else:
            logger.warning(
                "DevUITentacle %s: twitch for %s but no active sink — message dropped",
                self.id,
                key,
            )

    def open_stream(self, conversation: Conversation) -> StreamSink:
        # In practice DevUI always pre-registers its sink in the chat handler
        # before kicking, so the octopus's `get_or_open` falls back to the
        # pre-registered one and this factory isn't called. We still implement
        # the ABC contract — a fresh sink is harmless if invoked.
        return DevUIStreamSink()

    async def absorb(
        self, seg: ImageSegment, save_dir: Path, message_id: str
    ) -> None:
        return None

    async def secrete(self, seg: ImageSegment) -> None:
        return None
