from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pydantic_ai.tools import DeferredToolResults

from octomate.output import NativeAgentEvent
from octomate.schemas.actions import AgentMessage
from octomate.schemas.conversation import Conversation
from octomate.tentacles.base import Tentacle


class AgentTentacle(Tentacle, ABC):
    """Base class for agents following pydantic-ai run entrypoints."""

    @abstractmethod
    async def run(
        self,
        conversation: Conversation,
        user_prompt: str,
        *,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> list[AgentMessage]:
        """Run one logical conversation turn and return finalized messages."""

    @abstractmethod
    def run_stream(
        self,
        conversation: Conversation,
        user_prompt: str,
        *,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Run one turn and stream finalized messages."""

    @abstractmethod
    def run_stream_events(
        self,
        conversation: Conversation,
        user_prompt: str,
        *,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> AsyncIterator[NativeAgentEvent]:
        """Run one turn and stream native pydantic-ai events."""
