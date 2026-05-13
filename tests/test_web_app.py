from __future__ import annotations

import pytest
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import PartEndEvent, PartStartEvent, ToolCallPart
from pydantic_ai.ui.vercel_ai.request_types import SubmitMessage

from octomate import Octomate
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.web.dev_ui.adapter import GraphAdapter, OctomateVercelEventStream
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


async def test_dev_ui_stream_omits_structured_output_tool_chunks() -> None:
    stream = OctomateVercelEventStream(
        SubmitMessage(id="chat", messages=[]),
        sdk_version=6,
    )
    part = ToolCallPart(
        tool_name="final_result",
        args={"response": []},
        tool_call_id="call_final",
    )

    async def events():
        yield PartStartEvent(index=0, part=part)
        yield PartEndEvent(index=0, part=part)

    chunks = [chunk async for chunk in stream.transform_stream(events())]
    chunk_types = [chunk.type for chunk in chunks]

    assert "tool-input-start" not in chunk_types
    assert "tool-input-delta" not in chunk_types
    assert "tool-input-available" not in chunk_types


async def test_dev_ui_complete_chunks_render_agent_messages_as_text() -> None:
    result = AgentRunResult(
        [
            AgentMessage(
                segments=[TextSegment(data={"text": "Hello from Inkling"})]
            )
        ]
    )

    chunks = [chunk async for chunk in GraphAdapter.complete_chunks(result)]

    assert [chunk.type for chunk in chunks] == ["text-start", "text-delta", "text-end"]
    assert chunks[1].delta == "Hello from Inkling"
