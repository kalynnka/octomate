from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response

from octomate.managers.conversations import ConversationManager
from octomate.schemas.base import sqlalchemy_materia


@dataclass
class Octomate:
    """Application host for shared services and HTTP routers."""

    conversations: ConversationManager = field(default_factory=ConversationManager)
    agents: dict[str, Any] = field(default_factory=dict)
    routers: list[APIRouter] = field(default_factory=list)

    def register_agent(self, id: str, agent: Any) -> Any:
        if id in self.agents:
            raise ValueError(f"agent {id!r} already registered")
        self.agents[id] = agent
        return agent

    def include_router(self, router: APIRouter) -> APIRouter:
        self.routers.append(router)
        return router

    def app(self, *, title: str = "Octomate") -> FastAPI:
        app = FastAPI(title=title, docs_url="/docs", redoc_url=None)
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
