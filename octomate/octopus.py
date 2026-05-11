"""`Octomate` orchestrator — owns the event pipes + the FastAPI app."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import anyio
from fastapi import FastAPI, Request, Response

from octomate.managers.conversations import ConversationManager
from octomate.nerve import (
    AgentSignal,
    ChannelSignal,
    MessageBatch,
    Nerve,
    NerveDispatcher,
    SendSegments,
    SinkRegistry,
    StreamFrame,
)
from octomate.schemas.actions import AgentMessage
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import AgentInput
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel.base import ChannelTentacle

logger = logging.getLogger(__name__)


@dataclass
class Octomate:
    """The orchestrator + ASGI entrypoint of an octomate deployment.

    Owns the two `Nerve`s and their dispatchers, the channels and agents
    dicts, the per-conversation `SinkRegistry`, and the `ConversationManager`
    that resolves a `ConversationKey` to a DB-persisted row (which then
    routes inbound events to the right agent / outbound signals to the
    right channel).

    Public surface for tentacles:

        await octomate.kick(key, events)      # channels call on inbound
        await octomate.emit(signal)           # agents call on outbound
        octomate.sinks.set(key, sink)         # channels pre-register a sink

    Lifecycle:

        async with octomate.run(): ...        # spawn dispatchers + channels
        app = octomate.app()                  # FastAPI with lifespan wired up
    """

    conversations: ConversationManager = field(default_factory=ConversationManager)
    channels: dict[str, ChannelTentacle] = field(default_factory=dict)
    agents: dict[str, AgentTentacle] = field(default_factory=dict)
    channel_nerve: Nerve[ChannelSignal] = field(default_factory=Nerve)
    agent_nerve: Nerve[AgentSignal] = field(default_factory=Nerve)
    sinks: SinkRegistry = field(default_factory=SinkRegistry)

    def __post_init__(self) -> None:
        self.channel_dispatcher: NerveDispatcher[ChannelSignal] = NerveDispatcher(
            self.channel_nerve
        )
        self.agent_dispatcher: NerveDispatcher[AgentSignal] = NerveDispatcher(
            self.agent_nerve
        )
        self.channel_dispatcher.on("message_batch", self._on_message_batch)
        self.agent_dispatcher.on("send_segments", self._on_send_segments)
        self.agent_dispatcher.on("stream_frame", self._on_stream_frame)

    def connect(self, tentacle: Tentacle) -> None:
        """Register a tentacle with this orchestrator.

        Dispatches into `channels` / `agents` by tentacle subtype and binds
        the tentacle's `octopus` reference so it can `emit()` / `kick()`
        later. Raises if a tentacle with the same id is already attached.
        """
        if isinstance(tentacle, AgentTentacle):
            if tentacle.id in self.agents:
                raise ValueError(f"agent {tentacle.id!r} already attached")
            tentacle.octopus = self
            self.agents[tentacle.id] = tentacle
        elif isinstance(tentacle, ChannelTentacle):
            if tentacle.id in self.channels:
                raise ValueError(f"channel {tentacle.id!r} already attached")
            tentacle.octopus = self
            self.channels[tentacle.id] = tentacle
        else:
            raise TypeError(f"unknown tentacle type: {type(tentacle).__name__}")

    async def kick(
        self, key: ConversationKey, contents: list[AgentInput]
    ) -> None:
        """Inbound: channels call this with fresh `MessageEvent`s or a
        `ResumeEvent` for deferred-tool resolution."""
        await self.channel_nerve.send(MessageBatch(key=key, events=contents))

    async def emit(self, signal: AgentSignal) -> None:
        """Outbound: agents call this with `SendSegments` / `StreamFrame`."""
        await self.agent_nerve.send(signal)

    async def _on_message_batch(self, sig: MessageBatch) -> None:
        conv = await self.conversations.ensure(sig.key)
        agent_id = conv.agent_tentacle_id
        if agent_id is None or agent_id not in self.agents:
            logger.warning(
                "no agent grafted for conversation %s (agent_tentacle_id=%r); dropping batch",
                sig.key,
                agent_id,
            )
            return
        agent = self.agents[agent_id]
        await agent(conv, sig.events)

    async def _on_send_segments(self, sig: SendSegments) -> None:
        conv = await self.conversations.ensure(sig.target_key)
        channel = self.channels.get(conv.channel_tentacle_id)
        if channel is None:
            logger.warning(
                "no channel connected for conversation %s (channel_tentacle_id=%r); "
                "dropping SendSegments",
                sig.target_key,
                conv.channel_tentacle_id,
            )
            return
        await channel.twitch(sig.target_key, AgentMessage(segments=sig.segments))

    async def _on_stream_frame(self, sig: StreamFrame) -> None:
        conv = await self.conversations.ensure(sig.target_key)
        channel = self.channels.get(conv.channel_tentacle_id)
        if channel is None:
            logger.warning(
                "no channel connected for conversation %s; dropping StreamFrame",
                sig.target_key,
            )
            return
        sink = self.sinks.get_or_open(sig.target_key, lambda: channel.open_stream(conv))
        await sink.write(sig)
        if sig.frame_type == "close":
            await self.sinks.close(sig.target_key)

    @asynccontextmanager
    async def run(self) -> AsyncIterator[Octomate]:
        """Run the orchestrator: dispatchers + channel activations.

        - Agents are entered as async context managers (resource setup).
        - Both dispatchers run as tasks in the same group.
        - Every channel's `activate()` runs as a task.
        - On exit (normal or exceptional), the TaskGroup's cancel_scope
          cancels everything; agent context managers run their __aexit__
          via the AsyncExitStack.
        """
        async with AsyncExitStack() as stack, anyio.create_task_group() as tg:
            for agent in self.agents.values():
                await stack.enter_async_context(agent)
            tg.start_soon(self.channel_dispatcher.run)
            tg.start_soon(self.agent_dispatcher.run)
            for channel in self.channels.values():
                tg.start_soon(channel.activate)
            try:
                yield self
            finally:
                tg.cancel_scope.cancel()
                await self.sinks.close_all()

    def app(self, *, title: str = "Octomate") -> FastAPI:
        """Build the ASGI app: lifespan + materia + channel HTTP routes.

        Call this once tentacles are grafted/connected. The lifespan enters
        `self.run()` to spawn the dispatchers + WS-channel loops; the
        per-request middleware activates the arcanus materia inside each
        request task (the contextvar set inside lifespan doesn't propagate
        across ASGI request boundaries, so a separate copy is needed).
        Channels' `router()` is included BEFORE the lifespan starts since
        FastAPI freezes the router on startup.
        """

        @asynccontextmanager
        async def lifespan(_: FastAPI):
            with sqlalchemy_materia():
                async with self.run():
                    yield

        app = FastAPI(title=title, docs_url="/docs", redoc_url=None, lifespan=lifespan)
        app.state.octopus = self

        @app.middleware("http")
        async def activate_materia(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            with sqlalchemy_materia():
                return await call_next(request)

        for channel in self.channels.values():
            router = channel.router()
            if router is not None:
                app.include_router(router)

        return app
