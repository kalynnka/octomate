from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.config import MindConfig
from octomate.memory.base import OctopusMemory
from octomate.octopus import Octopus
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageSegment,
    TextSegment,
)
from octomate.schemas.session import UserProfile
from octomate.tentacles.base import SendTarget, Tentacle, PlatformMessage

BOT_USER_ID = "bot-001"
BOT_NAME = "TestBot"


class MockChromo:
    async def sip(self, raw: Any) -> MessageEvent | None:
        return None

    async def squirt(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]:
        return []


class MockInk:
    def inspect(self) -> UserProfile:
        return UserProfile(user_id=BOT_USER_ID, name=BOT_NAME)

    async def send_message(
        self, receive_id: str, receive_id_type: str, msg_type: str, content: str
    ) -> bool:
        return True

    async def reply_message(self, message_id: str, msg_type: str, content: str) -> bool:
        return True

    async def upload_image(self, data: bytes) -> str | None:
        return None

    async def download_image(
        self, message_id: str, file_key: str
    ) -> tuple[bytes, str] | None:
        return None

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, name=f"User-{user_id}")


class MockTentacle(Tentacle):
    sent: list[tuple[SendTarget, list[AgentSegment]]]
    confirmations_requested: int

    def __init__(self, tag: str, octopus: Octopus, flush_delay: float = 0.0) -> None:
        self.sent = []
        self.confirmations_requested = 0
        self.ink = MockInk()
        self.chromo = MockChromo()
        super().__init__(tag, octopus, flush_delay=flush_delay)

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
    ) -> bool:
        return True

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        pass

    async def secrete(self, seg: ImageSegment) -> None:
        pass

    async def send_confirmation(self, target: SendTarget, action: Any) -> bool:
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
    memory = OctopusMemory(store_path=Path("/tmp/.octomate_test_memory"))
    brain = MindConfig(model="gemini-pro", api_key="test-key")
    return Octopus(brain, memory)


def text_response_model(text: str) -> FunctionModel:
    """FunctionModel that always returns a single TextSegment AgentMessage."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        output_tool = next(
            (t for t in info.output_tools if "deferred" not in t.name.lower()),
            info.output_tools[0] if info.output_tools else None,
        )
        if output_tool:
            payload = json.dumps(
                [{"segments": [{"type": "text", "data": {"text": text}}]}]
            )
            return ModelResponse(
                parts=[ToolCallPart(tool_name=output_tool.name, args=payload)]
            )
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(fn)


def silent_model() -> FunctionModel:
    """FunctionModel that returns an empty AgentMessage list (silent response)."""

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        output_tool = next(
            (t for t in info.output_tools if "deferred" not in t.name.lower()),
            info.output_tools[0] if info.output_tools else None,
        )
        if output_tool:
            return ModelResponse(
                parts=[ToolCallPart(tool_name=output_tool.name, args="[]")]
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
