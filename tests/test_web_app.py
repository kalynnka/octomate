from __future__ import annotations

from typing import cast

import pytest
from fastapi.routing import APIRoute
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.ui.vercel_ai.response_types import DataChunk

from octomate import Octomate
from octomate.tentacles.agent.base import AgentTentacle
from octomate.web.dev_ui.adapter import GraphAdapter
from octomate.web.dev_ui import build_dev_ui_router


class FakeAgent:
    agent = object()


def test_dev_ui_router_is_bound_to_octomate_instance() -> None:
    octomate = Octomate()

    with pytest.raises(ValueError, match="registered agent"):
        build_dev_ui_router(octomate, agent_id="inkling")

    octomate.register_agent("inkling", cast(AgentTentacle, FakeAgent()))
    octomate.include_router(build_dev_ui_router(octomate, agent_id="inkling"))

    app = octomate.app()
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/chat" in paths
    assert "/api/configure" in paths


async def test_dev_ui_complete_chunks_emit_deferred_tool_requests() -> None:
    result = AgentRunResult(
        DeferredToolRequests(
            calls=[
                ToolCallPart(
                    tool_name="ask_user",
                    args={"question": "Name?"},
                    tool_call_id="call_ask",
                )
            ]
        )
    )

    chunks = [chunk async for chunk in GraphAdapter.complete_chunks(result)]
    data_chunks = [chunk for chunk in chunks if isinstance(chunk, DataChunk)]

    assert [chunk.type for chunk in data_chunks] == ["data-deferred-call"]
    assert data_chunks[0].data["toolName"] == "ask_user"
