from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
from fastapi import APIRouter, FastAPI, Request, Response

from octomate.managers.conversations import ConversationManager
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.graph import (
    ResponseTarget,
    TriageDeps,
    TriageState,
    triage_graph,
)
from octomate.tentacles.agent.graph.triage import RunTriage
from octomate.tentacles.channel.base import ChannelTentacle

logger = logging.getLogger(__name__)


@dataclass
class Octomate:
    """Application host for shared services, agents, channels, and routers."""

    conversations: ConversationManager = field(default_factory=ConversationManager)
    agents: dict[str, AgentTentacle] = field(default_factory=dict)
    channels: dict[str, ChannelTentacle] = field(default_factory=dict)
    routers: list[APIRouter] = field(default_factory=list)

    def register_agent(self, id: str, agent: AgentTentacle) -> AgentTentacle:
        if id in self.agents:
            raise ValueError(f"agent {id!r} already registered")
        agent.id = id
        agent.octomate = self
        self.agents[id] = agent
        return agent

    def connect_channel(self, id: str, channel: ChannelTentacle) -> ChannelTentacle:
        if id in self.channels:
            raise ValueError(f"channel {id!r} already registered")
        channel.id = id
        channel.octomate = self
        self.channels[id] = channel
        return channel

    def include_router(self, router: APIRouter) -> APIRouter:
        self.routers.append(router)
        return router

    async def kick(
        self,
        key: ConversationKey,
        contents: list[MessageEvent],
        *,
        agent_id: str | None = None,
    ) -> None:
        """Dispatch one channel-originated turn through triage and delivery."""
        if not contents:
            return
        channel = self.channels.get(key.channel_tentacle_id)
        if channel is None:
            raise ValueError(f"unknown channel {key.channel_tentacle_id!r}")

        with sqlalchemy_materia():
            conversation = await self.conversations.ensure(
                key,
                agent_tentacle_id=agent_id,
            )

            resolved_agent_id = agent_id or conversation.agent_tentacle_id
            if not resolved_agent_id:
                raise ValueError(f"conversation {key} has no agent assigned")
            agent_tentacle = self.agents.get(resolved_agent_id)
            if agent_tentacle is None:
                raise ValueError(f"unknown agent {resolved_agent_id!r}")

            try:
                user_prompt = "\n\n".join(str(event) for event in contents).strip()
                if not user_prompt:
                    return
                deps = TriageDeps(
                    agent=agent_tentacle,
                    conversation_manager=self.conversations,
                    source_target=ResponseTarget(
                        channel_id=key.channel_tentacle_id,
                        key=key,
                        thread_strategy=channel.thread_strategy,
                        mode="main",
                    ),
                    channels=self.channels,
                )
                await triage_graph.run(
                    RunTriage(
                        user_prompt=user_prompt,
                        message_history=list(conversation.messages),
                    ),
                    state=TriageState(),
                    deps=deps,
                )
            except Exception as exc:
                logger.exception(
                    "Agent %s failed while handling %s", resolved_agent_id, key
                )
                await channel.respond_text(
                    key,
                    f"Agent error: {exc}",
                )

    def app(self, *, title: str = "Octomate") -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            with sqlalchemy_materia():
                async with anyio.create_task_group() as tg:
                    for channel in self.channels.values():
                        tg.start_soon(channel.activate)
                    try:
                        yield
                    finally:
                        for channel in reversed(list(self.channels.values())):
                            try:
                                await channel.deactivate()
                            except Exception:
                                logger.exception(
                                    "Channel %s failed during shutdown",
                                    getattr(channel, "id", "<unknown>"),
                                )
                        tg.cancel_scope.cancel()

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
