from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.schemas.session import SessionKey
from octomate.managers.sessions import SessionManager
from octomate.tentacles.agent.inkling import build_inkling_agent
from octomate.tentacles.base import Octopus
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.dev_ui.adapter import GraphAdapter
from octomate.tentacles.channel.dev_ui.app import build_dev_ui_app
from octomate.tentacles.channel.dev_ui.chromo import StubChromo
from octomate.tentacles.channel.dev_ui.ink import StubInk

logger = logging.getLogger(__name__)


class DevUITentacle(ChannelTentacle):
    """Channel tentacle that serves pydantic-ai's chat UI and drives inkling_graph.

    Bypasses the standard `ingest -> octopus.kick` path: each /api/chat POST
    runs one inkling_graph invocation server-side, persisting messages to
    the RDB. The UI's `messages` array is ignored — only the conversation
    `id` (treated as the session key's chat_id) and the latest user prompt
    are read from the request.
    """

    sessions: SessionManager
    adapter: GraphAdapter

    # `feelers` is not set: the DevUI never invokes confirm/question/todo
    # feelers (round-trips happen via the Vercel approval protocol inside
    # GraphAdapter). Real feelers land when InteractionStore lands.

    def __init__(self, id: str, octopus: Octopus) -> None:
        self.ink = StubInk()
        self.chromo = StubChromo()
        super().__init__(id, octopus)
        self.sessions = SessionManager()
        self.adapter = GraphAdapter(
            tentacle_id=id,
            agent=build_inkling_agent(),
            sessions=self.sessions,
        )

    async def __call__(
        self, key: SessionKey, contents: list[MessageEvent]
    ) -> None:
        logger.info(
            "DevUI tentacle %s ignored kick (HTTP request is the dispatch): %s",
            self.id,
            key,
        )

    async def activate(self) -> None:
        return None

    async def deactivate(self) -> None:
        return None

    async def absorb(
        self, seg: ImageSegment, save_dir: Path, message_id: str
    ) -> None:
        return None

    async def secrete(self, seg: ImageSegment) -> None:
        return None

    @property
    def app(self) -> FastAPI:
        """FastAPI app exposing GET /, POST /api/chat, and Swagger at /docs."""
        return build_dev_ui_app(self.adapter)
