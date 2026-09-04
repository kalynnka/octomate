from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.routing import APIRoute
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.ui.vercel_ai.request_types import (
    SubmitMessage,
    TextUIPart,
    UIMessage,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config.channels import AgentModelConfig, VercelChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agents.inkling import InklingTentacle
from octomate.tentacles.agents.inkling.base import InklingOutput
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.channels.base import ChannelTentacle
from octomate.tentacles.channels.web.vercel import VercelTentacle, build_vercel_router
from octomate.tentacles.channels.web.vercel.base import ROUTE_SEP
from tests.support.agents import build_non_stream_agent, build_scripted_agent
from tests.support.managers import a_loaded_thread

# The dev UI drives one configured reception agent through octomate.kick.
RECEPTION_MODEL = "deepseek:deepseek-v4-pro"


async def _register(
    octomate: Octomate, agent: Agent[None, InklingOutput]
) -> VercelTentacle:
    assert agent.model is not None
    octomate.connect(
        InklingTentacle(
            "inkling",
            octomate,
            agent=agent,
            models={RECEPTION_MODEL: agent.model},
        )
    )
    channel = octomate.connect(
        VercelTentacle(
            "dev_ui",
            octomate,
            config=VercelChannelConfig(
                agents=[AgentModelConfig(agent="inkling", model=RECEPTION_MODEL)],
            ),
        )
    )
    assert isinstance(channel, VercelTentacle)
    await channel.probe()
    return channel


async def _post(
    channel: VercelTentacle,
    prompt: str,
    *,
    chat_id: str = "chat-1",
    selected_model: str | None = None,
) -> str:
    body = SubmitMessage(
        id=chat_id,
        messages=[
            UIMessage(
                id="m1",
                role="user",
                parts=[TextUIPart(type="text", text=prompt)],
            )
        ],
    )
    response = await channel.handle_request(body, selected_model=selected_model)
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

    channel = VercelTentacle(
        "dev_ui",
        octomate,
        config=VercelChannelConfig(
            agents=[AgentModelConfig(agent="inkling", model="test")]
        ),
    )
    assert isinstance(channel, ChannelTentacle)
    # connect mounts the channel's router (VercelTentacle.routers) — no manual include.
    octomate.connect(channel)

    app = octomate.app()
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/chat" in paths
    assert "/api/configure" in paths


async def test_handle_request_streams_scripted_reply(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = await _register(octomate, agent)

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
    channel = await _register(Octomate(), agent)

    payload = await _post(channel, "write a poem")

    assert _streamed_text(payload) == "".join(tokens)


async def test_handle_request_records_chat_ledger(
    in_memory_engine: AsyncEngine,
) -> None:
    """The dev UI follows the channel pattern: the inbound user message and the
    agent reply land in the shared thread ledger, not a separate history path."""
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = await _register(octomate, agent)

    await _post(channel, "hi there")

    address = ChannelAddress(
        channel_tentacle_id="dev_ui",
        chat_type="thread",
        chat_id="dev",
        user_id="dev",
        channel_thread_id="chat-1",
    )
    thread = await a_loaded_thread(octomate.thread_manager, address)
    messages = list(thread.messages)
    inbound = [message for message in messages if message.direction == "inbound"]
    outbound = [message for message in messages if message.direction == "outbound"]

    assert [message.message_text for message in inbound] == ["hi there"]
    assert [message.message_text for message in outbound] == ["all done!"]
    assert thread.active_agent_tentacle_id == "inkling"


async def _register_routes(
    octomate: Octomate, agents: dict[str, Agent[None, InklingOutput]]
) -> VercelTentacle:
    receptions = []
    for agent_id, agent in agents.items():
        assert agent.model is not None
        octomate.connect(
            InklingTentacle(
                agent_id, octomate, agent=agent, models={RECEPTION_MODEL: agent.model}
            )
        )
        receptions.append(AgentModelConfig(agent=agent_id, model=RECEPTION_MODEL))
    channel = octomate.connect(
        VercelTentacle(
            "dev_ui", octomate, config=VercelChannelConfig(agents=receptions)
        )
    )
    assert isinstance(channel, VercelTentacle)
    await channel.probe()
    return channel


def _dev_thread_address(thread_id: str = "chat-1") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="dev_ui",
        chat_type="thread" if thread_id else "dm",
        chat_id="dev",
        user_id="dev",
        channel_thread_id=thread_id,
    )


async def test_configure_lists_every_routable_agent() -> None:
    octomate = Octomate()
    inkling, _ = build_scripted_agent(["a"])
    claude, _ = build_scripted_agent(["b"])
    channel = await _register_routes(octomate, {"inkling": inkling, "claude": claude})

    ids = {
        f"{agent_config.agent}{ROUTE_SEP}{agent_config.model}"
        for agent_config in channel.routable_agents()
    }
    assert ids == {
        f"inkling{ROUTE_SEP}{RECEPTION_MODEL}",
        f"claude{ROUTE_SEP}{RECEPTION_MODEL}",
    }


async def test_selected_route_routes_to_and_owns_the_chosen_agent(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    inkling, _ = build_scripted_agent(["from inkling"])
    claude, _ = build_scripted_agent(["from claude"])
    channel = await _register_routes(octomate, {"inkling": inkling, "claude": claude})

    payload = await _post(
        channel, "hello", selected_model=f"claude{ROUTE_SEP}{RECEPTION_MODEL}"
    )

    assert "from claude" in payload
    thread = await octomate.thread_manager.ensure(_dev_thread_address())
    assert thread.active_agent_tentacle_id == "claude"


async def test_unchanged_selection_does_not_re_handoff(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    channel = await _register_routes(
        octomate,
        {"inkling": build_non_stream_agent(), "claude": build_non_stream_agent()},
    )
    selected = f"claude{ROUTE_SEP}{RECEPTION_MODEL}"

    await _post(channel, "one", selected_model=selected)
    await _post(channel, "two", selected_model=selected)

    thread = await octomate.thread_manager.ensure(_dev_thread_address())
    assert [handoff.to_agent_tentacle_id for handoff in thread.handoffs] == ["claude"]
