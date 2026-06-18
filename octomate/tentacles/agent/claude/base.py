from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, overload

import logfire
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from pydantic_ai import (
    AgentCapability,
    AgentModelSettings,
    AgentNativeTool,
    AgentRunResult,
    AgentRunResultEvent,
    RunUsage,
    UsageLimits,
)
from pydantic_ai.agent.abstract import (
    AgentInstructions,
    AgentMetadata,
    EventStreamHandler,
    RunOutputDataT,
)
from pydantic_ai.messages import UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from uuid_utils.compat import uuid7

from octomate.capabilities.deferred import DeferredSuspender
from octomate.capabilities.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import ClaudeCodeConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.agent.base import AgentSpecInput, AgentTentacle
from octomate.tentacles.agent.claude.adapter import ClaudeRunAccumulator

if TYPE_CHECKING:
    from octomate.base import Octomate


def _prompt_text(user_prompt: str | Sequence[UserContent] | None) -> str:
    """The plain-text prompt to send to the Claude CLI. Multimodal prompt parts
    are not supported yet; a prompt must carry at least some text."""
    if isinstance(user_prompt, str):
        text = user_prompt
    elif user_prompt:
        text = "\n".join(part for part in user_prompt if isinstance(part, str))
    else:
        text = ""
    if not text:
        raise ValueError("ClaudeCodeTentacle requires a non-empty text prompt")
    return text


@dataclass
class ClaudeCodeTentacle(AgentTentacle[str, None]):
    """Claude Agent SDK runner exposed as an Octomate agent tentacle.

    A run drives a `ClaudeSDKClient` locally (SSH transport lands later),
    translating its message stream through `ClaudeRunAccumulator` into live
    stream events (proxied to the channel feelers) and persisted
    `ModelMessage`s. The Claude session id is stored on the conversation as
    `external_id` and replayed via `resume=` so Claude owns its own
    context across turns. Output is the run's final text (`str`); pydantic-ai
    run options that don't map onto Claude (custom output_type, toolsets,
    capabilities, ...) are ignored.
    """

    config: ClaudeCodeConfig = field(init=False)

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: ClaudeCodeConfig,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config

    async def _iter_events(
        self,
        user_prompt: str | Sequence[UserContent] | None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        conversation = await self.octomate.conversations.ensure(
            conversation_key, agent_tentacle_id=self.id
        )
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(user_prompt)
        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=self.config.model or None,
            permission_mode="acceptEdits",
            max_turns=self.config.max_turns,
            resume=conversation.external_id,
        )
        with logfire.span(
            "ClaudeCodeTentacle {agent_id} {run_name} [{conversation_key}]",
            agent_id=self.id,
            run_name=run_name or "claude",
            conversation_key=str(conversation_key),
        ):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(_prompt_text(user_prompt))
                async for message in client.receive_response():
                    for event in accumulator.consume(message):
                        yield event
            run_id = str(uuid7())
            await self.octomate.conversations.record_agent_run(
                conversation,
                run_id=run_id,
                messages=accumulator.messages,
                name=run_name,
                external_id=accumulator.session_id,
            )
            yield AgentRunResultEvent(
                accumulator.build_result(
                    run_id=run_id, conversation_id=str(conversation.id)
                )
            )

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[str]: ...

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[RunOutputDataT]: ...

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        event_stream_handler: EventStreamHandler[None] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> AgentRunResult[str | RunOutputDataT]:
        result: AgentRunResult[str] | None = None
        async for event in self._iter_events(
            user_prompt, conversation_key=conversation_key, run_name=run_name
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("Claude run completed without a result")
        return result

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[str]: ...

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[RunOutputDataT]: ...

    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        deps: None = None,
        model_settings: AgentModelSettings[None] | None = None,
        usage_limits: UsageLimits | None = None,
        usage: RunUsage | None = None,
        metadata: AgentMetadata[None] | None = None,
        output_retries: int | None = None,
        infer_name: bool = True,
        toolsets: Sequence[AbstractToolset[None]] | None = None,
        builtin_tools: Sequence[AgentNativeTool[None]] | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        spec: AgentSpecInput | None = None,
    ) -> ReactEventStream[str | RunOutputDataT]:
        return ReactEventStream(
            self._iter_events(
                user_prompt, conversation_key=conversation_key, run_name=run_name
            )
        )
