"""The Vercel web channel: adapts pydantic-ai's Vercel AI protocol so the dev UI
drives Octomate through `octomate.kick`, sharing triage, ownership, and history
with the IM channels.

`octomate.kick` pushes output through feelers, but the dev UI needs an inline SSE
response. So `handle_request` runs the kick in a background task with a
per-request `current_sink` active; the channel's timeline feeler forwards the run
stream into it, and `handle_request` transcodes that to Vercel SSE. Keyed as a
`flat_thread` channel with a fixed thread id, every turn skips triage and streams
straight through the reception agent.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, ClassVar, Literal, TypeAlias, cast

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.vercel_ai.request_types import RequestData, TextUIPart
from pydantic_ai.ui.vercel_ai.response_types import BaseChunk

from octomate.capabilities.events import StreamEvents
from octomate.config import ChannelConfig
from octomate.config.channels import AgentModelConfig
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ChannelAddress, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment, TextSegment
from octomate.schemas.thread import Thread
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    IMMessageID,
    Ink,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.deferred import ApprovalFeeler, QuestionFeeler
from octomate.tentacles.channel.feelers.output import TimelineState
from octomate.tentacles.channel.web.vercel.event_stream import VercelEventStream

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

VercelStreamItem: TypeAlias = (
    StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
)

# The dev UI is single-user; the sender is fixed. Each Vercel chat (its `id`)
# is its own thread, so "new chat" in the UI starts a fresh Octomate thread.
DEV_USER_ID = "dev"

# Separator joining agent and model into the route id the UI picker offers.
ROUTE_SEP = ":"

# Per-request output sink the kick's presenter streams into. The channel is a
# singleton across concurrent requests, so each request sets its own sink here.
current_sink: ContextVar[MemoryObjectSendStream[VercelStreamItem] | None]
current_sink = ContextVar("vercel_output_sink", default=None)


class ChatExtra(BaseModel):
    """The selection fields the dev UI adds to the Vercel chat POST beyond the
    typed `RequestData` — currently the route id picked in the model picker."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model: str | None = None


class VercelSeamNotWired(NotImplementedError):
    """Raised by the platform send/encode seam, unused by the inline-SSE path."""

    def __init__(self) -> None:
        super().__init__(
            "The Vercel channel streams inline over SSE; the platform "
            "send/encode seam is unused by the dev-UI delivery path."
        )


class VercelInk(Ink[BaseChunk]):
    """Transport stub: only identity probing is used (output streams inline)."""

    async def inspect(self) -> UserProfile:
        return UserProfile(user_id="dev_ui", name="Octomate")

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id or DEV_USER_ID, name="Dev")

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
    """Inbound translation only; outbound encoding is unused."""

    async def sip(self, raw: RequestData) -> MessageEvent | None:
        message = next(
            (
                m
                for m in reversed(raw.messages)
                if m.role == "user"
                and any(isinstance(p, TextUIPart) and p.text for p in m.parts)
            ),
            None,
        )
        if message is None:
            return None
        text = "".join(
            part.text for part in message.parts if isinstance(part, TextUIPart)
        )
        return MessageEvent(
            message_id=message.id,
            thread_id=raw.id,
            user_id=DEV_USER_ID,
            chat_id=DEV_USER_ID,
            chat_type="private",
            segments=[TextSegment(data={"text": text})],
            raw=text,
        )

    def outbound_markdown(self, text: str) -> list[BaseChunk]:
        raise VercelSeamNotWired


class VercelTimelineState(TimelineState):
    """Forwards the raw run stream to the request's sink (the UI renders tokens
    itself), bypassing the default markdown-rotation lifecycle."""

    def __init__(
        self,
        address: ChannelAddress,
        ask_questions: QuestionFeeler,
        approvals: ApprovalFeeler,
        sink: MemoryObjectSendStream[VercelStreamItem],
    ) -> None:
        self.address = address
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.sink = sink
        self.message_id = None
        self.reply_to = None

    async def drive(
        self,
        stream: AsyncIterator[VercelStreamItem],
    ) -> None:
        async for event in stream:
            await self.sink.send(event)


class VercelTimelineFeeler:
    """Opens a per-run timeline that streams into the active request's sink."""

    def __init__(
        self, *, ask_questions: QuestionFeeler, approvals: ApprovalFeeler
    ) -> None:
        self.ask_questions = ask_questions
        self.approvals = approvals

    @asynccontextmanager
    async def open(
        self, address: ChannelAddress
    ) -> AsyncGenerator[VercelTimelineState, None]:
        sink = current_sink.get()
        if sink is None:
            raise RuntimeError("vercel timeline opened without an active request sink")
        yield VercelTimelineState(address, self.ask_questions, self.approvals, sink)


class VercelTentacle(ChannelTentacle[RequestData, BaseChunk]):
    """Channel that serves the pydantic-ai Vercel dev UI over `octomate.kick`."""

    SDK_VERSION: ClassVar[Literal[6]] = 6
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"

    def __init__(self, id: str, octomate: Octomate, *, config: ChannelConfig) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=VercelInk(),
            chromo=VercelChromo(),
            config=config,
        )
        self.feelers.timeline = VercelTimelineFeeler(
            ask_questions=self.feelers.ask_questions,
            approvals=self.feelers.approvals,
        )
        # Last route the UI picker selected, per chat. A pick only re-routes when
        # it changes; otherwise an in-thread summon owner stands (hybrid).
        self.selected_routes: dict[str, str] = {}

    def routers(self) -> tuple[APIRouter]:
        """The dev-UI HTTP surface, mounted by `Octomate.connect`."""
        # Local import: routes.py imports this module, so a module-level import cycles.
        from octomate.tentacles.channel.web.vercel.routes import build_vercel_router

        return (build_vercel_router(self.octomate, channel_id=self.id),)

    def routable_agents(self) -> list[AgentModelConfig]:
        """The agent-model routes the UI offers and can summon — the configured
        agents whose agent tentacle is registered."""
        return [
            agent_config
            for agent_config in self.config.agents
            if agent_config.agent in self.octomate.agents
        ]

    async def claim_selected_route(
        self, thread: Thread, chat_id: str, selected_model: str | None
    ) -> None:
        """Hybrid routing: a UI route pick claims thread ownership only when it
        changes from the last pick for this chat — otherwise an in-thread summon
        owner stands. The chosen agent stays summon-able like any other."""
        if selected_model is None or selected_model == self.selected_routes.get(
            chat_id
        ):
            return
        chosen = next(
            (
                agent_config
                for agent_config in self.routable_agents()
                if f"{agent_config.agent}{ROUTE_SEP}{agent_config.model}" == selected_model
            ),
            None,
        )
        if chosen is None:
            return
        self.selected_routes[chat_id] = selected_model
        if (thread.active_agent_tentacle_id, thread.active_model) == (
            chosen.agent,
            chosen.model,
        ):
            return
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=chosen.agent
        )
        await self.octomate.thread_manager.record_handoff(
            thread,
            from_agent_tentacle_id=thread.active_agent_tentacle_id,
            to_agent_tentacle_id=chosen.agent,
            to_model=chosen.model,
            reason="dev UI route selection",
            target_conversation_id=conversation.id,
        )

    async def handle_request(
        self, body: RequestData, *, selected_model: str | None = None
    ) -> StreamingResponse:
        event = await self.chromo.sip(body)
        if event is None:
            raise ValueError("Vercel request carried no user message")
        event.tentacle_id = self.id
        event.self_id = self.profile.user_id
        event.sender = await self.get_user_profile(event.user_id)
        thread = await self.octomate.thread_manager.ensure(
            ChannelAddress(
                channel_tentacle_id=self.id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                user_id=event.user_id,
                thread_id=event.thread_id,
            )
        )
        await self.claim_selected_route(thread, body.id, selected_model)
        thread_message = await self.octomate.thread_manager.record_inbound(event)

        send, receive = anyio.create_memory_object_stream[VercelStreamItem](128)
        captured: list[Exception] = []

        async def pump() -> None:
            token = current_sink.set(send)
            try:
                await self.octomate.kick(
                    UserMessageSignal(
                        [event], trigger_thread_message_id=thread_message.id
                    )
                )
            except Exception as exc:
                captured.append(exc)
            finally:
                current_sink.reset(token)
                await send.aclose()

        async def events() -> AsyncIterator[VercelStreamItem]:
            async with receive:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(pump)
                    async for event_out in receive:
                        yield event_out
            for error in captured:
                raise error

        stream = VercelEventStream(body, sdk_version=self.SDK_VERSION)
        return stream.streaming_response(
            stream.transform_stream(
                # pydantic-ai types the stream over its native events only; the
                # octomate events pass through at runtime and VercelEventStream
                # handles them.
                cast(AsyncIterator[NativeEvent], events())
            )
        )
