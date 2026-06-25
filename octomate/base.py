from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TypeVar

import logfire
from fastapi import APIRouter, FastAPI, Request, Response

from octomate.managers.channel import ChannelThreadManager
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import (
    AwakeSignal,
    DeferredActionBatchResponse,
    UserMessageSignal,
)
from octomate.schemas.base import sqlalchemy_materia
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.triage import (
    Awake,
    TriageDeps,
    TriageState,
    triage_graph,
)

TentacleT = TypeVar("TentacleT", bound=Tentacle)
logger = logging.getLogger(__name__)


@dataclass
class Octomate:
    """Application host for shared services, agents, channels, and routers."""

    channel_threads: ChannelThreadManager = field(default_factory=ChannelThreadManager)
    conversations: ConversationManager = field(default_factory=ConversationManager)
    deferred_actions: DeferredActionManager = field(
        default_factory=DeferredActionManager
    )
    agents: dict[str, AgentTentacle] = field(default_factory=dict)
    channels: dict[str, ChannelTentacle] = field(default_factory=dict)
    routers: list[APIRouter] = field(default_factory=list)

    def connect(self, tentacle: TentacleT) -> TentacleT:
        if isinstance(tentacle, ChannelTentacle):
            tentacle.octomate = self
            if tentacle.id in self.channels:
                raise ValueError(f"channel {tentacle.id!r} already connected")
            self.channels[tentacle.id] = tentacle
            return tentacle
        if isinstance(tentacle, AgentTentacle):
            tentacle.octomate = self
            if tentacle.id in self.agents:
                raise ValueError(f"agent {tentacle.id!r} already connected")
            self.agents[tentacle.id] = tentacle
            return tentacle
        logger.warning(
            "Skipping unknown tentacle %s (%s)",
            tentacle.id,
            type(tentacle).__name__,
        )
        return tentacle

    def include_router(self, router: APIRouter) -> APIRouter:
        self.routers.append(router)
        return router

    async def kick(
        self,
        signal: AwakeSignal,
    ) -> None:
        """Trigger the agent graph from a user message turn or deferred response."""
        with logfire.span(
            "kick {signal_type}", signal_type=type(signal).__name__
        ) as span:
            if isinstance(signal, UserMessageSignal) and signal:
                address = signal.address
                span.set_attribute("channel_id", address.channel_tentacle_id)
                span.set_attribute("conversation_address", str(address))
            elif isinstance(signal, DeferredActionBatchResponse):
                span.set_attribute("batch_id", str(signal.batch_id))
                # Deliver the response to a live Claude run blocked on this batch
                # (approval/question), rather than resuming through the graph.
                for agent in self.agents.values():
                    if not agent.in_process:
                        continue
                    future = agent.pending.get(signal.batch_id)
                    if future is None:
                        continue
                    if not future.done():
                        future.set_result(signal)
                    span.set_attribute("resolved_live", agent.id)
                    return
            with sqlalchemy_materia():
                await triage_graph.run(
                    Awake(signal=signal),
                    state=TriageState(),
                    deps=TriageDeps(
                        agents=self.agents,
                        channels=self.channels,
                        conversation_manager=self.conversations,
                        action_manager=self.deferred_actions,
                    ),
                )

    def app(self, *, title: str = "Octomate") -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            with sqlalchemy_materia():
                # Each tentacle is an async context manager owning its own
                # long-lived resources (agents: warm MCP sessions; channels:
                # the inbound receive loop). Enter agents first so their tools
                # are warm before channels start ingesting; the stack tears
                # everything down in reverse on shutdown.
                async with AsyncExitStack() as stack:
                    for agent in self.agents.values():
                        await stack.enter_async_context(agent)
                    for channel in self.channels.values():
                        await stack.enter_async_context(channel)
                    yield

        app = FastAPI(title=title, docs_url="/docs", redoc_url=None, lifespan=lifespan)
        app.state.octomate = self

        @app.middleware("http")
        async def activate_materia(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            with sqlalchemy_materia():
                return await call_next(request)

        for router in self.routers:
            app.include_router(router)

        return app
