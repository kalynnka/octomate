from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic_ai.models import infer_model
from pydantic_ai.ui._web.app import _get_ui_html
from pydantic_ai.ui.vercel_ai.request_types import RequestData

from octomate.tentacles.channel.web.vercel.base import VercelTentacle

if TYPE_CHECKING:
    from octomate import Octomate


def build_vercel_router(
    octomate: Octomate,
    *,
    channel_id: str = "dev_ui",
) -> APIRouter:
    """Build the Vercel dev-UI HTTP router bound to a registered VercelTentacle."""
    registered = octomate.channels.get(channel_id)
    if not isinstance(registered, VercelTentacle):
        raise ValueError(
            f"Vercel router requires a registered VercelTentacle at {channel_id!r}"
        )
    channel: VercelTentacle = registered

    router = APIRouter()

    async def _serve_index() -> HTMLResponse:
        content = await _get_ui_html(None)
        return HTMLResponse(
            content=content,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return await _serve_index()

    @router.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # Quiet the browser's automatic favicon probe.
        return Response(status_code=204)

    @router.post(
        "/api/chat",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Vercel AI Data Stream Protocol — drives react_graph",
    )
    async def chat(body: RequestData) -> StreamingResponse:
        return await channel.handle_request(body)

    @router.options("/api/chat", include_in_schema=False)
    async def chat_options() -> Response:
        # CORS preflight — the bundled chat UI hits this even on same-origin.
        return Response()

    @router.get("/api/configure", include_in_schema=False)
    async def configure() -> JSONResponse:
        # Populates the chat UI's model + builtin-tool pickers. Our agent is
        # wired internally (model + toolset are fixed) so we surface a single
        # read-only entry showing the active agent and its underlying LLM.
        # The UI still requires at least one model on init — an empty list
        # makes it crash dereferencing the default. Whatever the user picks
        # here is ignored on the server; this entry is informational.
        agent = channel.graph_agent
        assert agent.model is not None, "agent must be constructed with a model"
        return JSONResponse(
            {
                "models": [
                    {
                        "id": channel.agent_id,
                        "name": (
                            f"{channel.agent_id} "
                            f"({infer_model(agent.model).model_name})"
                        ),
                        "builtinTools": [],
                    }
                ],
                "builtinTools": [],
            }
        )

    @router.get("/api/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    # The chat UI is a SPA that pushes `/<chat_id>` into the URL after the
    # first user message. Refresh / direct-link to that URL must also serve
    # the index HTML so the client-side router can pick up.
    @router.get("/{chat_id}", response_class=HTMLResponse, include_in_schema=False)
    async def index_alias(chat_id: str) -> HTMLResponse:
        return await _serve_index()

    return router
