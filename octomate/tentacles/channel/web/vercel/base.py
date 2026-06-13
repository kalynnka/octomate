"""The Vercel web channel — a `ChannelTentacle` that adapts pydantic-ai's Vercel
AI event protocol so the bundled dev UI can drive the react graph.

This is the minimal first step toward the full web channel: it keeps today's
direct react-graph drive (no triage / `octomate.kick` yet) but lives inside the
channel-tentacle shape so the web surface is registered in `octomate.channels`
and shares the channel conventions. The response is request-scoped — each
`handle_request` returns a `StreamingResponse` driven inline.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from fastapi.responses import StreamingResponse
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from pydantic_ai.ui.vercel_ai.request_types import RequestData
from pydantic_ai.ui.vercel_ai.response_types import BaseChunk

from octomate.capabilities.agent import Agent
from octomate.capabilities.react import (
    ReactDeps,
    ReactState,
    ReactStreamEvent,
    ResumeTurn,
    StartTurn,
    iter_react_graph_events,
)
from octomate.config import ChannelConfig
from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.tentacles.channel.base import (
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    IMMessageID,
    Ink,
)
from octomate.tentacles.channel.web.vercel.event_stream import OctomateUIEventStream

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


class VercelSeamNotWired(NotImplementedError):
    """Raised by transport/chromo members the direct-drive path never touches."""

    def __init__(self) -> None:
        super().__init__(
            "The Vercel channel renders inline over SSE via the Vercel adapter and "
            "OctomateUIEventStream; the chromo/transport seam wires in when the "
            "channel moves onto octomate.kick."
        )


class VercelInk(Ink[BaseChunk]):
    """Transport stub for the Vercel channel.

    The direct-drive path never pushes through the ink (it streams inline), so
    only `inspect`/`get_user_profile` carry static dev identities; the genuinely
    platform-shaped members fail fast until kick-dispatch lands.
    """

    async def inspect(self) -> UserProfile:
        return UserProfile(user_id="dev_ui", name="Inkling")

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id or "dev", name="Dev")

    async def upload_media(self, data: bytes) -> str | None:
        raise VercelSeamNotWired

    async def download_image(
        self, seg: ImageSegment, message_id: str
    ) -> DownloadedImage | None:
        raise VercelSeamNotWired

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[BaseChunk],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        raise VercelSeamNotWired


class VercelChromo(Chromo[RequestData, BaseChunk]):
    """Translation stub for the Vercel channel.

    Inbound decode runs through `handle_request` (the Vercel adapter) and
    outbound encode through `OctomateUIEventStream` in this step, so the chromo
    seam is latent until kick-dispatch lands.
    """

    async def sip(self, raw: RequestData) -> MessageEvent | None:
        raise VercelSeamNotWired

    def outbound_markdown(self, text: str) -> list[BaseChunk]:
        raise VercelSeamNotWired


class VercelTentacle(ChannelTentacle[RequestData, BaseChunk]):
    """Channel that serves the pydantic-ai Vercel dev UI over the react graph."""

    SDK_VERSION: ClassVar[Literal[6]] = 6

    ink: VercelInk
    chromo: VercelChromo

    def __init__(self, id: str, octomate: Octomate, *, config: ChannelConfig) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=VercelInk(),
            chromo=VercelChromo(),
            config=config,
        )

    @property
    def graph_agent(self) -> Agent[None, InklingOutput]:
        tentacle = self.octomate.agents.get(self.agent_id)
        agent = getattr(tentacle, "agent", None)
        if agent is None:
            raise ValueError(
                f"Vercel channel requires registered agent {self.agent_id!r} "
                "to expose a pydantic-ai agent"
            )
        return cast(Agent[None, InklingOutput], agent)

    async def handle_request(self, body: RequestData) -> StreamingResponse:
        agent = self.graph_agent
        adapter = VercelAIAdapter(
            agent=agent,
            run_input=body,
            sdk_version=self.SDK_VERSION,
        )
        key = ConversationKey(
            channel_tentacle_id=self.id,
            chat_type="private",
            chat_id=body.id,
            user_id="dev",
            thread_id="",
        )
        deferred = adapter.deferred_tool_results
        client_messages = adapter.sanitize_messages(
            adapter.messages,
            deferred_tool_results=deferred,
        )

        event_stream = OctomateUIEventStream(body, sdk_version=self.SDK_VERSION)
        return event_stream.streaming_response(
            event_stream.transform_stream(
                # pydantic-ai types the stream over its native events only; the
                # octomate events pass through at runtime and OctomateUIEventStream
                # handles them.
                cast(
                    AsyncIterator[NativeEvent],
                    self.native_events(
                        conversation_key=key,
                        user_prompt=self.latest_user_prompt(client_messages),
                        deferred=deferred,
                    ),
                )
            )
        )

    async def native_events(
        self,
        *,
        conversation_key: ConversationKey,
        user_prompt: str | Sequence[UserContent] | None,
        deferred: DeferredToolResults | None,
    ) -> AsyncIterator[ReactStreamEvent[InklingOutput]]:
        try:
            async for event in iter_react_graph_events(
                self.start_node(user_prompt=user_prompt, deferred=deferred),
                state=ReactState(
                    conversation_key=conversation_key,
                    agent_tentacle_id=self.agent_id,
                ),
                deps=ReactDeps(
                    agent=self.graph_agent,
                    conversation_manager=self.octomate.conversations,
                    agent_deps=None,
                ),
            ):
                yield event
        except Exception:
            logger.exception("Vercel channel graph stream failed")
            raise

    @staticmethod
    def start_node(
        *,
        user_prompt: str | Sequence[UserContent] | None,
        deferred: DeferredToolResults | None,
    ) -> StartTurn | ResumeTurn:
        if deferred is not None:
            return ResumeTurn(deferred_results=deferred)
        return StartTurn(user_prompt=user_prompt)

    @staticmethod
    def latest_user_prompt(
        messages: Sequence[ModelMessage],
    ) -> str | Sequence[UserContent] | None:
        for message in reversed(messages):
            if not isinstance(message, ModelRequest):
                continue
            contents = [
                part.content
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            if not contents:
                continue
            if len(contents) == 1:
                return contents[0]

            combined: list[UserContent] = []
            for content in contents:
                if isinstance(content, str):
                    combined.append(content)
                else:
                    combined.extend(content)
            return combined
        return None
