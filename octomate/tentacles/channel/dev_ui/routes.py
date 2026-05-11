"""DevUI HTTP routes — packaged as an `APIRouter` for `Octomate.app()` to include.

The bundled `@pydantic/ai-chat-ui` SPA expects:
- `GET /` and `GET /{chat_id}` → the same UI HTML (client-side router).
- `GET /favicon.ico` → 204 quiets the browser's auto-probe.
- `POST /api/chat` → Vercel AI Data Stream Protocol over SSE.
- `OPTIONS /api/chat` → CORS preflight.
- `GET /api/configure` → models + builtin-tools (read-only here).
- `GET /api/health` → liveness ping.

Handlers reach the orchestrator via `request.app.state.octopus`, the same
path future Slack/Lark card-action handlers will take.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic_ai.models import infer_model
from pydantic_ai.ui._web.app import _get_ui_html
from pydantic_ai.ui.vercel_ai._event_stream import VERCEL_AI_DSP_HEADERS
from pydantic_ai.ui.vercel_ai.request_types import RequestData, SubmitMessage

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import AgentInput, MessageEvent, ResumeEvent
from octomate.schemas.segments import TextSegment
from octomate.tentacles.channel.dev_ui.adapter import (
    DevUIStreamSink,
    extract_deferred_results,
    extract_user_text,
)

if TYPE_CHECKING:
    from octomate.tentacles.channel.dev_ui.base import DevUITentacle

logger = logging.getLogger(__name__)


def build_dev_ui_router(channel: DevUITentacle) -> APIRouter:
    """Build the DevUI `APIRouter`; `Octomate.app()` includes it on startup."""
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

    @router.get("/{chat_id}", response_class=HTMLResponse, include_in_schema=False)
    async def index_alias(chat_id: str) -> HTMLResponse:
        return await _serve_index()

    @router.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @router.options("/api/chat", include_in_schema=False)
    async def chat_options() -> Response:
        return Response()

    @router.post(
        "/api/chat",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Vercel AI Data Stream Protocol — drives the agent via octopus.kick",
    )
    async def chat(body: RequestData, request: Request) -> StreamingResponse:
        octopus = request.app.state.octopus
        if not isinstance(body, SubmitMessage):
            return StreamingResponse(
                _noop_stream(),
                media_type="text/event-stream",
                headers=VERCEL_AI_DSP_HEADERS,
            )

        chat_id = body.id
        key = ConversationKey(
            channel_tentacle_id=channel.id,
            chat_type="private",
            chat_id=chat_id,
            user_id="dev",
            thread_id="",
        )
        await octopus.conversations.ensure(key, agent_tentacle_id=channel.agent_id)

        user_text = extract_user_text(body.messages)
        deferred = extract_deferred_results(body.messages)

        sink = DevUIStreamSink()
        octopus.sinks.set(key, sink)

        batch: list[AgentInput]
        if deferred is not None:
            batch = [ResumeEvent(payload=deferred)]
        else:
            batch = [
                MessageEvent(
                    tentacle_id=channel.id,
                    user_id="dev",
                    chat_id=chat_id,
                    chat_type="private",
                    segments=[TextSegment(data={"text": user_text})],
                )
            ]
        await octopus.kick(key, batch)

        return StreamingResponse(
            sink.iter_sse(),
            media_type="text/event-stream",
            headers=VERCEL_AI_DSP_HEADERS,
        )

    @router.get("/api/configure", include_in_schema=False)
    async def configure(request: Request) -> JSONResponse:
        octopus = request.app.state.octopus
        agent = octopus.agents.get(channel.agent_id)
        assert agent is not None, "agent missing for configured channel"
        model_name = infer_model(agent.agent.model).model_name
        return JSONResponse(
            {
                "models": [
                    {
                        "id": channel.agent_id,
                        "name": f"{channel.agent_id} ({model_name})",
                        "builtinTools": [],
                    }
                ],
                "builtinTools": [],
            }
        )

    @router.get("/api/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    return router


async def _noop_stream():
    from pydantic_ai.ui.vercel_ai.response_types import DoneChunk, StartChunk

    sdk_version = 6
    yield f"data: {StartChunk().encode(sdk_version)}\n\n".encode()
    yield f"data: {DoneChunk().encode(sdk_version)}\n\n".encode()
