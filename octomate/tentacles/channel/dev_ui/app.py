from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic_ai.models import infer_model
from pydantic_ai.ui._web.app import _get_ui_html
from pydantic_ai.ui.vercel_ai.request_types import RequestData

from octomate.schemas.base import sqlalchemy_materia
from octomate.tentacles.channel.dev_ui.adapter import GraphAdapter


def build_dev_ui_app(adapter: GraphAdapter) -> FastAPI:
    """FastAPI app: chat UI at /, Vercel AI POST at /api/chat, Swagger at /docs."""
    app = FastAPI(
        title="Octomate DevUI",
        docs_url="/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def activate_materia(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        with sqlalchemy_materia():
            return await call_next(request)

    async def _serve_index() -> HTMLResponse:
        content = await _get_ui_html(None)
        return HTMLResponse(
            content=content,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return await _serve_index()

    # The chat UI is a SPA that pushes `/<chat_id>` into the URL after the
    # first user message. Refresh / direct-link to that URL must also serve
    # the index HTML so the client-side router can pick up. Excluded from
    # the schema so it doesn't shadow /api/* in /docs.
    @app.get("/{chat_id}", response_class=HTMLResponse, include_in_schema=False)
    async def index_alias(chat_id: str) -> HTMLResponse:
        return await _serve_index()

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # Quiet the browser's automatic favicon probe.
        return Response(status_code=204)

    @app.post(
        "/api/chat",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Vercel AI Data Stream Protocol — drives inkling_graph",
    )
    async def chat(body: RequestData) -> StreamingResponse:
        return await adapter.handle_request(body)

    @app.options("/api/chat", include_in_schema=False)
    async def chat_options() -> Response:
        # CORS preflight — the bundled chat UI hits this even on same-origin.
        return Response()

    @app.get("/api/configure", include_in_schema=False)
    async def configure() -> JSONResponse:
        # Populates the chat UI's model + builtin-tool pickers. Our agent is
        # wired internally (model + toolset are fixed) so we surface a single
        # read-only entry showing the active agent and its underlying LLM.
        # The UI still requires at least one model on init — an empty list
        # makes it crash dereferencing the default. Whatever the user picks
        # here is ignored on the server; this entry is informational.
        #
        # `agent.model` is statically typed `Model | KnownModelName | str | None`;
        # `infer_model` is idempotent on Model instances and narrows the type
        # for the static checker so `.model_name` resolves cleanly.
        assert adapter.agent.model is not None, "agent must be constructed with a model"
        return JSONResponse(
            {
                "models": [
                    {
                        "id": adapter.agent_id,
                        "name": f"{adapter.agent_id} ({infer_model(adapter.agent.model).model_name})",
                        "builtinTools": [],
                    }
                ],
                "builtinTools": [],
            }
        )

    @app.get("/api/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app
