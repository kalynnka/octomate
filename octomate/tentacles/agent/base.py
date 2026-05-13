from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic_ai import (
    AgentBuiltinTool,
    AgentCapability,
    AgentEventStream,
    AgentModelSettings,
    AgentRunResult,
    AgentSpec,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent.abstract import AgentInstructions, AgentMetadata, EventStreamHandler
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset

from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.base import Tentacle


class AgentTentacle(Tentacle, ABC):
    """Base class for Octomate agents wrapping pydantic-ai run entrypoints."""

    @abstractmethod
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        output_type: OutputSpec[Any] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[Any] = None,
        deps: Any = None,
        model_settings: AgentModelSettings[Any] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[Any] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[Any]] | None = None,
        builtin_tools: Sequence[AgentBuiltinTool[Any]] | None = None,
        event_stream_handler: EventStreamHandler[Any] | None = None,
        capabilities: Sequence[AgentCapability[Any]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AgentRunResult[Any]:
        """Run the agent for an Octomate conversation."""

    @abstractmethod
    def run_stream(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        output_type: OutputSpec[Any] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[Any] = None,
        deps: Any = None,
        model_settings: AgentModelSettings[Any] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[Any] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[Any]] | None = None,
        builtin_tools: Sequence[AgentBuiltinTool[Any]] | None = None,
        event_stream_handler: EventStreamHandler[Any] | None = None,
        capabilities: Sequence[AgentCapability[Any]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AsyncIterator[StreamedRunResult[Any, Any]]:
        """Stream the agent output for an Octomate conversation."""

    @abstractmethod
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        output_type: OutputSpec[Any] | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[Any] = None,
        deps: Any = None,
        model_settings: AgentModelSettings[Any] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[Any] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[Any]] | None = None,
        builtin_tools: Sequence[AgentBuiltinTool[Any]] | None = None,
        capabilities: Sequence[AgentCapability[Any]] | None = None,
        spec: dict[str, Any] | AgentSpec | None = None,
    ) -> AgentEventStream[Any]:
        """Stream raw agent events for an Octomate conversation."""
