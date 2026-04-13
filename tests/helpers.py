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
from octomate.agents.pulse import PulseAgents
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
from octomate.schemas.session import SessionKey, UserProfile
from octomate.stores.interaction import InteractionStore
from octomate.stores.message import MessageStore
from octomate.stores.thread import ThreadStore
from octomate.tentacles.base import ChannelTentacle, PlatformMessage
from octomate.tentacles.feelers import NULL_FEELERS
from octomate.transmuters.messages import Message
from octomate.transmuters.threads import Thread

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


class MockThreadStore(ThreadStore):
    """In-memory thread store for tests — no database required."""

    async def get(self, key: SessionKey) -> Thread:
        cached = self._get_cached_thread(key)
        if cached is not None:
            return cached
        thread = Thread(
            tentacle_id=key.tentacle_id,
            user_id=key.user_id,
            group_id=key.group_id,
            thread_id=key.thread_id,
            chat_id=key.chat_id,
            owner_tentacle=self.default_owner,
        )
        self._set_cached_thread(key, thread)
        return thread

    async def set_owner(self, key: SessionKey, owner: Any) -> None:
        thread = await self.get(key)
        updated = thread.model_copy(update={"owner_tentacle": owner.id})
        self.thread_cache.pop(key, None)
        self._set_cached_thread(key, updated)


class MockMessageStore(MessageStore):
    """In-memory message store for tests — no database required."""

    _messages: list[Message]

    def __init__(self) -> None:
        self._messages = []

    async def save_all(self, records: list[Message]) -> None:
        self._messages.extend(records)

    async def find_by_ids(self, message_ids: list[str]) -> dict[str, Message]:
        return {m.message_id: m for m in self._messages if m.message_id in message_ids}

    async def list_recent(
        self, key: SessionKey, size: int | None = None
    ) -> list[Message]:
        chat = key.group_id or key.user_id
        matches = [
            m
            for m in self._messages
            if m.tentacle_id == key.tentacle_id and m.chat == chat
        ]
        if size is not None:
            return matches[-size:]
        return matches


class MockOctopusMemory(OctopusMemory):
    """OctopusMemory backed by MockMessageStore — no database required."""

    def __init__(self) -> None:
        self.messages = MockMessageStore()


class MockTentacle(ChannelTentacle):
    sent: list[tuple[SessionKey, list[AgentSegment]]]
    confirmations_requested: int

    def __init__(self, tag: str, octopus: Octopus, flush_delay: float = 0.0) -> None:
        self.sent = []
        self.confirmations_requested = 0
        self.ink = MockInk()
        self.chromo = MockChromo()
        self.feelers = NULL_FEELERS
        memory = MockOctopusMemory()
        from octomate.transmuters.interactions import Todo

        triage = Agent(
            "test",
            deps_type=SessionContext,
            output_type=[list[AgentMessage], list[Todo], DeferredToolRequests],
        )
        step = Agent("test", deps_type=SessionContext, output_type=str)
        synth = Agent(
            "test", deps_type=SessionContext, output_type=list[AgentMessage]
        )
        agents = PulseAgents(triage=triage, step=step, synthesize=synth)
        super().__init__(tag, octopus, agents, memory, flush_delay=flush_delay)
        self.threads = MockThreadStore(default_owner=tag)
        self.interactions = InteractionStore(threads=self.threads)

    def inject(self, event: MessageEvent) -> None:
        event.tentacle_id = self.id
        self.buffer.push(event)

    async def activate(self) -> None:
        pass

    async def deactivate(self) -> None:
        pass

    async def twitch(self, key: SessionKey, segments: list[AgentSegment]) -> None:
        self.sent.append((key, list(segments)))

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

    async def send_confirmation(self, key: SessionKey, action: Any) -> bool:
        _ = key, action
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
    return Octopus()


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
    """Run both dispatchers as background tasks, cancel on exit."""
    from octomate.transmuters import sqlalchemy_materia

    with sqlalchemy_materia:
        channel_task = asyncio.create_task(octopus.channel_dispatcher.run())
        agent_task = asyncio.create_task(octopus.agent_dispatcher.run())
        try:
            yield
        finally:
            channel_task.cancel()
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channel_task
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task
