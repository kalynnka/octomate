from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import SessionContext
from octomate.agents.surge import create_surge_agent
from octomate.config import SurgeConfig
from octomate.memory.base import OctopusMemory
from octomate.octopus import Octopus
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageSegment,
    TextSegment,
)
from octomate.schemas.session import UserProfile
from octomate.tentacles.base import PlatformMessage, SendTarget, Tentacle
from octomate.tentacles.feelers import NULL_FEELERS

BOT_USER_ID = "bot-001"
BOT_NAME = "TestBot"


class MockChromo:
    async def sip(self, raw: Any) -> MessageEvent | None:
        _ = raw
        return None

    async def squirt(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]:
        _ = segments, reply_to
        return []


class MockInk:
    def inspect(self) -> UserProfile:
        return UserProfile(user_id=BOT_USER_ID, name=BOT_NAME)

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, name=f"User-{user_id}")

    async def upload_media(self, data: bytes) -> str | None:
        _ = data
        return None

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        _ = resource_id, kwargs
        return None


class MockTentacle(Tentacle):
    sent: list[tuple[SendTarget, list[AgentSegment]]]
    confirmations_requested: int

    def __init__(self, tag: str, octopus: Octopus, flush_delay: float = 0.0) -> None:
        self.sent = []
        self.confirmations_requested = 0
        self.ink = MockInk()
        self.chromo = MockChromo()
        self.feelers = NULL_FEELERS
        memory = OctopusMemory(store_path=Path("/tmp/.octomate_test_memory"))
        flick = Agent(
            "test",
            deps_type=SessionContext,
            output_type=[list[AgentMessage], DeferredToolRequests],
        )
        super().__init__(tag, octopus, flick, memory, flush_delay=flush_delay)

    def inject(self, event: MessageEvent) -> None:
        event.tentacle_id = self.tag
        self.buffer.push(event)

    async def activate(self) -> None:
        pass

    async def deactivate(self) -> None:
        pass

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        self.sent.append((target, list(segments)))

    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        _ = chat_id, chat_type, messages, reply_to, reply_in_thread
        return None

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        _ = seg, save_dir, message_id

    async def secrete(self, seg: ImageSegment) -> None:
        _ = seg

    async def send_confirmation(self, target: SendTarget, action: Any) -> bool:
        _ = target, action
        self.confirmations_requested += 1
        return False


def make_private_event(user_id: str = "user-1", text: str = "hello") -> MessageEvent:
    return MessageEvent(
        timestamp=float(int(time.time())),
        message_id=f"msg-{user_id}-prv",
        user_id=user_id,
        chat_id=user_id,
        chat_type="private",
        sender=UserProfile(user_id=user_id, name=f"User-{user_id}"),
        segments=[TextSegment(data={"text": text})],
        raw=text,
    )


def make_group_event(
    user_id: str = "user-1",
    group_id: str = "group-1",
    text: str = "hello",
    mention_bot: bool = False,
) -> MessageEvent:
    segments: list = []
    if mention_bot:
        segments.append(AtSegment(data=AtData(user_id=BOT_USER_ID, name=BOT_NAME)))
    segments.append(TextSegment(data={"text": text}))
    return MessageEvent(
        timestamp=float(int(time.time())),
        message_id=f"msg-{user_id}-grp",
        user_id=user_id,
        chat_id=group_id,
        chat_type="group",
        sender=UserProfile(user_id=user_id, name=f"User-{user_id}"),
        segments=segments,
        raw=text,
    )


def make_octopus() -> Octopus:
    surge_config = SurgeConfig(model="gemini-pro", api_key="test-key")
    agent = create_surge_agent(surge_config)
    return Octopus(agent)


def text_response_model(text: str) -> FunctionModel:
    """FunctionModel that always returns a single TextSegment AgentMessage."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        _ = messages
        output_tool = next(
            (t for t in info.output_tools if "deferred" not in t.name.lower()),
            info.output_tools[0] if info.output_tools else None,
        )
        if output_tool:
            payload = json.dumps(
                {"response": [{"segments": [{"type": "text", "data": {"text": text}}]}]}
            )
            return ModelResponse(
                parts=[ToolCallPart(tool_name=output_tool.name, args=payload)]
            )
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(fn)


def silent_model() -> FunctionModel:
    """FunctionModel that returns an empty AgentMessage list (silent response)."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        _ = messages
        output_tool = next(
            (t for t in info.output_tools if "deferred" not in t.name.lower()),
            info.output_tools[0] if info.output_tools else None,
        )
        if output_tool:
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name=output_tool.name, args='{"response": []}')
                ]
            )
        return ModelResponse(parts=[TextPart(content="")])

    return FunctionModel(fn)


@contextlib.asynccontextmanager
async def rolling_loop(octopus: Octopus):
    """Run octopus.rolling() as a background task, cancel on exit."""
    task = asyncio.create_task(octopus.rolling())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
