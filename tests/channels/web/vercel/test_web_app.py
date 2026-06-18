from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.routing import APIRoute
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.ui.vercel_ai.request_types import (
    SubmitMessage,
    TextUIPart,
    UIMessage,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.agent import Agent
from octomate.config.channels import VercelChannelConfig
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agent.inkling import InklingTentacle
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.web.vercel import VercelTentacle, build_vercel_router

from tests.support.agents import build_scripted_agent


def _register(octomate: Octomate, agent: Agent[None, InklingOutput]) -> VercelTentacle:
    octomate.register_agent(
        "reception",
        InklingTentacle(
            "reception",
            octomate,
            agent=agent,
            conversation_manager=octomate.conversations,
        ),
    )
    channel = octomate.connect_channel(
        "dev_ui",
        VercelTentacle("dev_ui", octomate, config=VercelChannelConfig()),
    )
    assert isinstance(channel, VercelTentacle)
    return channel


async def _post(channel: VercelTentacle, prompt: str) -> str:
    body = SubmitMessage(
        id="chat-1",
        messages=[
            UIMessage(
                id="m1",
                role="user",
                parts=[TextUIPart(type="text", text=prompt)],
            )
        ],
    )
    response = await channel.handle_request(body)
    parts: list[bytes] = []
    async for part in response.body_iterator:
        parts.append(part.encode() if isinstance(part, str) else bytes(part))
    return b"".join(parts).decode()


def _streamed_text(payload: str) -> str:
    """Concatenate the `text-delta` chunks from a Vercel SSE payload."""
    deltas: list[str] = []
    for line in payload.splitlines():
        if not line.startswith("data: "):
            continue
        body = line.removeprefix("data: ")
        if body == "[DONE]":
            continue
        chunk = json.loads(body)
        if chunk.get("type") == "text-delta":
            deltas.append(chunk["delta"])
    return "".join(deltas)


def test_vercel_router_requires_registered_channel() -> None:
    octomate = Octomate()

    with pytest.raises(ValueError, match="VercelTentacle"):
        build_vercel_router(octomate, channel_id="dev_ui")

    channel = VercelTentacle("dev_ui", octomate, config=VercelChannelConfig())
    assert isinstance(channel, ChannelTentacle)
    octomate.connect_channel("dev_ui", channel)
    octomate.include_router(build_vercel_router(octomate, channel_id="dev_ui"))

    app = octomate.app()
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/chat" in paths
    assert "/api/configure" in paths


async def test_handle_request_streams_scripted_reply(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = _register(octomate, agent)

    payload = await _post(channel, "hi there")

    assert "all done!" in payload


async def test_handle_request_streams_every_text_delta(
    in_memory_engine: AsyncEngine,
) -> None:
    """A token-streaming text reply must reach the UI in full — not just the
    first token before the model's FinalResultEvent."""
    tokens = ["Another", " day", " dawns", " bright", "."]

    async def stream_text(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        for token in tokens:
            yield token

    agent: Agent[None, InklingOutput] = Agent(
        FunctionModel(stream_function=stream_text, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        system_prompt=SYSTEM_PROMPT,
    )
    channel = _register(Octomate(), agent)

    payload = await _post(channel, "write a poem")

    assert _streamed_text(payload) == "".join(tokens)
