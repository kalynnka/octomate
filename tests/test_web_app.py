from __future__ import annotations

import pytest
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests

from octomate import Octomate
from octomate.web.dev_ui.adapter import GraphAdapter
from octomate.web.dev_ui import build_dev_ui_router


class FakeAgent:
    model = None


def test_dev_ui_router_is_bound_to_octomate_instance() -> None:
    octomate = Octomate()

    with pytest.raises(ValueError, match="registered agent"):
        build_dev_ui_router(octomate, agent_id="inkling")

    octomate.register_agent("inkling", FakeAgent())
    octomate.include_router(build_dev_ui_router(octomate, agent_id="inkling"))

    app = octomate.app()
    paths = {route.path for route in app.routes}
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

    assert [chunk.type for chunk in chunks] == ["data-deferred-call"]
    assert chunks[0].data["toolName"] == "ask_user"
