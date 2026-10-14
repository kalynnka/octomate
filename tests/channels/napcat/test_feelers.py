from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import (
    MessageSentEvent,
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    TodoCreatedEvent,
)
from octomate.config import NapcatChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    FileData,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplySegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.napcat import NapcatTentacle
from octomate.types.json import JsonObject
from tests.support.channels import drive
from tests.support.scenarios import ChannelScript, play, streamed_text


@pytest.fixture
async def napcat_output() -> AsyncIterator[
    tuple[NapcatTentacle, ChannelAddress, list[JsonObject]]
]:
    channel = NapcatTentacle(
        "napcat",
        Octomate(),
        config=NapcatChannelConfig(
            agents=["inkling"],
            ws_url="ws://napcat.test",
            http_url="http://napcat.test",
        ),
    )
    await channel.ink.httpx.aclose()
    posts: list[JsonObject] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(TypeAdapter(JsonObject).validate_json(request.content))
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": "msg-1"}},
        )

    async with httpx.AsyncClient(
        base_url=channel.ink.http_url, transport=httpx.MockTransport(respond)
    ) as client:
        channel.ink.httpx = client
        yield (
            channel,
            ChannelAddress("napcat", "group", "2002", "3003"),
            posts,
        )


@pytest.mark.parametrize("streamed", [True, False])
async def test_timeline_preserves_native_segments(
    napcat_output: tuple[NapcatTentacle, ChannelAddress, list[JsonObject]],
    tmp_path: Path,
    streamed: bool,
) -> None:
    channel, address, posts = napcat_output
    image = tmp_path / "pic.png"
    image.write_bytes(b"image-bytes")
    file = tmp_path / "report.pdf"
    file.write_bytes(b"file-bytes")
    segments: list[MessageSegment] = [
        ReplySegment(data={"id": "1001"}),
        AtSegment(data=AtData(user_id="3003", name="Alice")),
        MarkdownSegment(data={"text": "hello **Alice**"}),
        ImageSegment(data=ImageData(file=str(image))),
        FileSegment(data=FileData(file=str(file), name="report.pdf")),
    ]
    events: ChannelScript = (
        [ResultSegmentEvent(segment=segment) for segment in segments]
        if streamed
        else []
    )
    events.append(AgentRunResultEvent(AgentRunResult(segments)))

    assert await drive(channel, address, play(events)) == "msg-1"

    assert posts == [
        {
            "group_id": "2002",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "at", "data": {"qq": "3003"}},
                {"type": "text", "data": {"text": "hello Alice"}},
                {
                    "type": "image",
                    "data": {
                        "file": f"base64://{base64.b64encode(b'image-bytes').decode()}"
                    },
                },
                {
                    "type": "file",
                    "data": {
                        "file": f"base64://{base64.b64encode(b'file-bytes').decode()}",
                        "name": "report.pdf",
                    },
                },
            ],
        }
    ]


async def test_timeline_preserves_text_chunks_and_todos_between_segments(
    napcat_output: tuple[NapcatTentacle, ChannelAddress, list[JsonObject]],
) -> None:
    channel, address, posts = napcat_output
    events: ChannelScript = [
        TodoCreatedEvent(
            todo=Todo(conversation_id=uuid7(), ref="1", content="plan the trip")
        ),
        ResultTextDeltaEvent(delta="before **"),
        ResultTextDeltaEvent(delta="Alice**"),
        ResultSegmentEvent(segment=AtSegment(data=AtData(user_id="3003"))),
        ResultTextDeltaEvent(delta="after **"),
        ResultTextDeltaEvent(delta="Alice**"),
    ]

    await drive(channel, address, play(events))

    assert posts == [
        {
            "group_id": "2002",
            "message": [
                {"type": "text", "data": {"text": "before Alice"}},
                {"type": "at", "data": {"qq": "3003"}},
                {
                    "type": "text",
                    "data": {"text": "after Alice\n\nTasks:\n- [ ] plan the trip"},
                },
            ],
        }
    ]


async def test_timeline_rotates_native_notice_before_final_answer(
    napcat_output: tuple[NapcatTentacle, ChannelAddress, list[JsonObject]],
) -> None:
    channel, address, posts = napcat_output
    events: ChannelScript = [
        MessageSentEvent(
            segments=[
                AtSegment(data=AtData(user_id="3003")),
                MarkdownSegment(data={"text": "**working**"}),
            ]
        ),
        *streamed_text("**done", "**"),
    ]

    await drive(channel, address, play(events))

    assert posts == [
        {
            "group_id": "2002",
            "message": [
                {"type": "at", "data": {"qq": "3003"}},
                {"type": "text", "data": {"text": "working"}},
            ],
        },
        {
            "group_id": "2002",
            "message": [{"type": "text", "data": {"text": "done"}}],
        },
    ]


@pytest.mark.parametrize("structured", [True, False])
async def test_inkling_output_reaches_napcat(
    napcat_output: tuple[NapcatTentacle, ChannelAddress, list[JsonObject]],
    structured: bool,
) -> None:
    channel, address, posts = napcat_output

    async def respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if not structured:
            yield "hello **"
            yield "Alice**"
            return
        segments = [
            {"type": "reply", "data": {"id": "1001"}},
            {"type": "at", "data": {"user_id": "3003", "name": "Alice"}},
            {"type": "markdown", "data": {"text": "hello **Alice**"}},
        ]
        yield {
            0: DeltaToolCall(
                name=info.output_tools[0].name,
                json_args=json.dumps({"response": segments}),
            )
        }

    inkling = InklingTentacle(
        "inkling",
        Octomate(),
        models={
            "scripted": FunctionModel(stream_function=respond, model_name="scripted")
        },
        system_prompt=SYSTEM_PROMPT,
    )

    await drive(channel, address, inkling.agent.stream_events("Reply to Alice."))

    message: list[JsonObject] = [{"type": "text", "data": {"text": "hello Alice"}}]
    if structured:
        message = [
            {"type": "reply", "data": {"id": "1001"}},
            {"type": "at", "data": {"qq": "3003"}},
            *message,
        ]
    assert posts == [{"group_id": "2002", "message": message}]
