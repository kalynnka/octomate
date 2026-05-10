from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic_ai.ui._web.app import _get_ui_html
from pydantic_ai.ui.vercel_ai.request_types import RequestData

from octomate.schemas.base import sqlalchemy_materia
from octomate.tentacles.channel.dev_ui.adapter import GraphAdapter


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Activate the arcanus materia for the lifetime of the app.

    Required so that store queries that go through arcanus' AsyncSession
    (entity adaption, transmuter↔ORM bridging) resolve correctly.
    """
    with sqlalchemy_materia:
        yield


def build_dev_ui_app(adapter: GraphAdapter) -> FastAPI:
    """FastAPI app: chat UI at /, Vercel AI POST at /api/chat, Swagger at /docs."""
    app = FastAPI(
        title="Octomate DevUI",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        content = await _get_ui_html(None)
        return HTMLResponse(
            content=content,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.post(
        "/api/chat",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Vercel AI Data Stream Protocol — drives inkling_graph",
    )
    async def chat(body: RequestData) -> StreamingResponse:
        return await adapter.handle_request(body)

    return app
