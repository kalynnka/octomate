from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, ClassVar, get_args, overload

import anyio
from pydantic import SecretStr, ValidationError
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
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import TextContent, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from rich.style import Style
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import ZcodeConfig
from octomate.prompts import tagged
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.messages import ModelRequest
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle
from octomate.tentacles.locks import SessionLocks
from octomate.tentacles.zcode.adapter import ZcodeRunAccumulator
from octomate.tentacles.zcode.client import ZcodeClient
from octomate.tentacles.zcode.wire import (
    DesktopConfig,
    SessionMessages,
    SessionSnapshot,
)
from octomate.types.json import JsonObject
from octomate.types.permissions import ZcodePermissionMode, is_zcode_mode

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


class ZcodeTentacle(AgentTentacle[str, None]):
    """Core driven sessions using ZCode's bundled desktop runtime."""

    config: ZcodeConfig
    conversation_locks: SessionLocks
    permission_modes: ClassVar[tuple[str, ...]] = get_args(ZcodePermissionMode)
    brand_color: ClassVar[Style | None] = Style(color="#3B6FF5", bold=True)
    description: str = (
        "ZCode coding agent for repository-aware software engineering tasks."
    )

    def __init__(self, id: str, octomate: Octomate, *, config: ZcodeConfig) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.gateway = False
        self.pending = {}
        # Widen the literal keys to the shared route vocabulary; dict(...) cannot do that.
        self.claims = {model: claim for model, claim in config.claims.items()}  # noqa: C416
        self.models = {model: model for model in config.models}
        self.conversation_locks = SessionLocks()

    @property
    def default_permission_mode(self) -> str:
        return self.config.permission_mode

    async def iter_events(
        self,
        user_prompt: str | Sequence[UserContent] | None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None,
        source_thread_address: ChannelAddress | None,
        source_thread_message_ids: Sequence[uuid.UUID] | None,
        run_name: str | None,
        output_type: OutputSpec[RunOutputDataT] | None,
        model: Model | KnownModelName | str | None,
        effort: ThinkingEffort | None,
        conversation_id: uuid.UUID | None,
        interactive: bool,
        instructions: AgentInstructions[None],
        capabilities: Sequence[AgentCapability[None]] | None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        if output_type is not None:
            raise ValueError("ZcodeTentacle supports text output only")
        if isinstance(user_prompt, str):
            prompt = user_prompt
        elif user_prompt is not None:
            text: list[str] = []
            for part in user_prompt:
                if isinstance(part, str):
                    text.append(part)
                elif isinstance(part, TextContent):
                    text.append(part.content)
                else:
                    raise ValueError("ZcodeTentacle supports text input only")
            prompt = "\n".join(text)
        else:
            prompt = ""
        if not prompt.strip():
            raise ValueError("ZcodeTentacle requires a non-empty text prompt")
        model_name = model.model_name if isinstance(model, Model) else model
        if model_name is None:
            if len(self.models) != 1:
                raise ValueError(
                    "Select a model for a ZCode agent with multiple configured models"
                )
            model_name = next(iter(self.models))
        if model_name not in self.config.models:
            raise ValueError(f"ZCode model {model_name!r} is not configured")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("ZcodeTentacle supports string instructions only")
        sent_prompt = (
            f"{tagged('instructions', instructions)}\n\n{prompt}"
            if instructions
            else prompt
        )
        desktop = await asyncio.to_thread(
            DesktopConfig.read, self.config.desktop_config
        )
        runtime_model = desktop.runtime_model(self.config.provider, model_name, effort)
        provider = desktop.provider[self.config.provider]
        missing = self.config.models - provider.models.keys()
        if missing:
            raise ValueError(
                f"ZCode desktop provider is missing configured models: {', '.join(sorted(missing))}"
            )
        secrets = [
            provider.options.api_key,
            *[SecretStr(value) for value in provider.options.headers.values()],
        ]
        if conversation_id is None:
            conversation = await self.octomate.conversations.ensure(
                thread_id, agent_tentacle_id=self.id
            )
            conversation_id = conversation.id
        async with self.conversation_locks.hold(str(conversation_id)):
            # Re-read after acquiring the lock: the preceding turn may have created its session.
            conversation = await self.octomate.conversations.get(conversation_id)
            if (
                conversation.thread_id != thread_id
                or conversation.agent_tentacle_id != self.id
            ):
                raise ValueError(
                    f"conversation {conversation_id} does not belong to ({self.id!r}, {thread_id})"
                )
            mode = (
                conversation.permission_mode
                if is_zcode_mode(conversation.permission_mode)
                else self.config.permission_mode
            )
            project = await self.run_project(thread_id)
            workspace = self.octomate.workspaces.open(thread_id, project)
            input_id = str(uuid7())
            accumulator = ZcodeRunAccumulator(
                prompt, input_id=input_id, model=model_name
            )
            run_id = str(uuid7())
            session_id: str | None = None
            async with (
                workspace,
                ZcodeClient(
                    self.config.command,
                    cwd=workspace.path,
                    state_dir=self.config.state_dir.resolve(),
                    request_timeout=self.config.request_timeout,
                    secrets=secrets,
                ) as client,
            ):
                workspace_ref: JsonObject = {
                    "workspacePath": str(workspace.path),
                    "workspaceKey": str(workspace.path),
                }
                try:
                    params: JsonObject = {
                        "workspace": workspace_ref,
                        "runtimeModel": runtime_model,
                    }
                    if conversation.external_id:
                        params["sessionId"] = conversation.external_id
                        snapshot = SessionSnapshot.model_validate(
                            await client.call("session/resume", params)
                        )
                    else:
                        params.update({"mode": mode, "titleGenerationEnabled": False})
                        snapshot = SessionSnapshot.model_validate(
                            await client.call("session/create", params)
                        )
                    session_id = snapshot.session.session_id
                    await client.call(
                        "session/setMode", {"sessionId": session_id, "mode": mode}
                    )
                    await client.call(
                        "session/subscribe",
                        {"sessionId": session_id, "deliveryKind": "desktop-continuous"},
                    )
                    await client.call(
                        "session/send",
                        {
                            "sessionId": session_id,
                            "inputId": input_id,
                            "content": sent_prompt,
                        },
                    )
                    while not accumulator.ended:
                        event = await client.events.get()
                        if isinstance(event, AgentRunError):
                            raise event
                        if event.session_id == session_id:
                            for translated in accumulator.consume(event):
                                yield translated
                    history = SessionMessages.model_validate(
                        await client.call("session/messages", {"sessionId": session_id})
                    )
                    accumulator.reconcile(history)
                    if accumulator.error:
                        raise AgentRunError(client.redact(accumulator.error))
                except ValidationError as error:
                    details = "; ".join(
                        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                        for item in error.errors(
                            include_input=False, include_context=False
                        )[:3]
                    )
                    raise AgentRunError(
                        f"ZCode returned an invalid session or event payload: {client.redact(details)}"
                    ) from None
                finally:
                    with anyio.CancelScope(shield=True):
                        if session_id is not None:
                            if not accumulator.ended and client.failure is None:
                                try:
                                    await client.call(
                                        "session/stop", {"sessionId": session_id}
                                    )
                                except AgentRunError as error:
                                    # Cleanup still persists the partial run and closes the owned process.
                                    logger.warning(
                                        "Could not stop ZCode session: %s", error
                                    )
                            recorded = (
                                await self.octomate.conversations.record_agent_run(
                                    conversation,
                                    run_id=run_id,
                                    messages=accumulator.messages,
                                    name=run_name,
                                    cwd=workspace.path,
                                    external_id=session_id,
                                )
                            )
                            if source_thread_message_ids:
                                if recorded is None:
                                    raise RuntimeError(
                                        "prompt-source bindings require a persisted ZCode run"
                                    )
                                request = next(
                                    (
                                        message
                                        for message in recorded.messages
                                        if isinstance(message, ModelRequest)
                                        and message.role == "user"
                                    ),
                                    None,
                                )
                                if request is None:
                                    raise RuntimeError(
                                        "prompt-source bindings require a persisted user ModelRequest"
                                    )
                                ids = list(source_thread_message_ids)
                                await self.octomate.thread_manager.bind_messages(
                                    ids,
                                    request.id,
                                    kind="request_source",
                                    run_id=recorded.id,
                                )
                                source_thread = (
                                    await self.octomate.thread_manager.ensure(
                                        source_thread_address or conversation_address
                                    )
                                )
                                await (
                                    self.octomate.thread_manager.advance_prompt_cursor(
                                        source_thread, ids[-1]
                                    )
                                )
            yield AgentRunResultEvent(
                accumulator.build_result(
                    run_id=run_id, conversation_id=str(conversation_id)
                )
            )

    @overload
    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        if deferred_tool_results is not None:
            raise ValueError("ZcodeTentacle does not support deferred tool results")
        result: AgentRunResult[str] | None = None
        async for event in self.iter_events(
            user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=source_thread_message_ids,
            run_name=run_name,
            output_type=output_type,
            model=model,
            effort=effort,
            conversation_id=conversation_id,
            interactive=interactive,
            instructions=instructions,
            capabilities=capabilities,
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("ZCode run completed without a result")
        return result

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: ThinkingEffort | None = None,
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        if deferred_tool_results is not None:
            raise ValueError("ZcodeTentacle does not support deferred tool results")
        return ReactEventStream(
            self.iter_events(
                user_prompt,
                conversation_address=conversation_address,
                thread_id=thread_id,
                source_thread_address=source_thread_address,
                source_thread_message_ids=source_thread_message_ids,
                run_name=run_name,
                output_type=output_type,
                model=model,
                effort=effort,
                conversation_id=conversation_id,
                interactive=interactive,
                instructions=instructions,
                capabilities=capabilities,
            )
        )
