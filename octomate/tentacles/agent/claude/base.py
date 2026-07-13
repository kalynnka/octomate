from __future__ import annotations

import asyncio
import contextlib
import uuid
import weakref
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import (
    TYPE_CHECKING,
    ClassVar,
    cast,
    overload,
)

import logfire
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    PreToolUseHookInput,
    ToolPermissionContext,
)
from pydantic import TypeAdapter
from pydantic.json_schema import JsonSchemaValue
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
from pydantic_ai.messages import ToolCallPart, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from uuid_utils.compat import uuid7

from octomate.capabilities.deferred import DeferredSuspender
from octomate.capabilities.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import ClaudeCodeConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
    ConversationPermissionMode,
)
from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    DeferredActionBatch,
    QuestionRequest,
)
from octomate.schemas.messages import ModelRequest
from octomate.tentacles.agent.base import AgentSpecInput, AgentTentacle
from octomate.tentacles.agent.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.agent.claude.hooks import CLAUDE_HOOK_PATH, ClaudeHookInput
from octomate.tentacles.agent.claude.ingest import ClaudeHookIngest
from octomate.tentacles.agent.claude.restore import ClaudeSessionRestore
from octomate.tentacles.agent.claude.transport import SSHTransport
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.base import Octomate


# Our snake_case approval levels → the Claude SDK's camelCase `permission_mode`.
SDK_PERMISSION_MODE: dict[ConversationPermissionMode, PermissionMode] = {
    "default": "default",
    "accept_edits": "acceptEdits",
    "bypass_permissions": "bypassPermissions",
}


@dataclass
class ClaudeCodeTentacle(AgentTentacle[str, None]):
    """Claude Agent SDK runner exposed as an Octomate agent tentacle.

    A run drives a `ClaudeSDKClient` — a local subprocess, or the remote
    `SSHTransport` when `config.ssh` is set — translating its message
    stream through `ClaudeRunAccumulator` into live
    stream events (proxied to the channel feelers) and persisted
    `ModelMessage`s. The Claude session id is stored on the conversation as
    `external_id` and replayed via `resume=` so Claude owns its own
    context across turns. Output is the run's final text (`str`); pydantic-ai
    run options that don't map onto Claude (custom output_type, toolsets,
    capabilities, ...) are ignored.
    """

    config: ClaudeCodeConfig = field(init=False)

    # A Claude run stays live in-process; `pending` (from `AgentTentacle`) parks a
    # waiter per gated tool / question until `Octomate.kick` delivers the response.
    in_process: ClassVar[bool] = True

    description: str = (
        "Coding agent for software engineering and multi-step technical work in a "
        "code repository."
    )

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: ClaudeCodeConfig,
        description: str | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.description = description or self.description
        self.pending = {}
        # One live Claude client per conversation (keyed by thread_id — this
        # tentacle owns one agent id, so a thread names its conversation): a new
        # turn interrupts the prior run for the same thread (Phase 6). Weak values
        # so a finished run's client drops out on its own once it is collected.
        self.live_clients: weakref.WeakValueDictionary[uuid.UUID, ClaudeSDKClient] = (
            weakref.WeakValueDictionary()
        )
        self.models = {model: model for model in config.models}

    def routers(self) -> tuple[APIRouter]:
        """The tentacle's HTTP surface, mounted by `Octomate.connect`: the hook router
        native Claude clients (app / CLI / VSCode) POST their session events into.
        `claude_hook_settings` is the client-side fragment that points a session at it.
        """
        return (self.hook_router,)

    @cached_property
    def session_restore(self) -> ClaudeSessionRestore:
        """Rebuilds native sessions' full model timelines from their transcripts.
        Shared (cached) so the fire-and-forget rebuild a finished session triggers and
        a later web open dedup against one another's in-flight work."""
        return ClaudeSessionRestore(self.octomate)

    @cached_property
    def hook_router(self) -> APIRouter:
        """The route behind `routers()`; cached so it is built once. The ingest reads
        the same managers this tentacle writes, through the already-bound `octomate`."""
        ingest = ClaudeHookIngest(self.octomate, self.session_restore)
        router = APIRouter(tags=["claude"])

        @router.post(
            CLAUDE_HOOK_PATH,
            summary="Claude Code hook pipe — streams a native session's human ledger in",
        )
        async def receive_hook(event: ClaudeHookInput) -> JSONResponse:
            await ingest.handle(event)
            # Claude Code reads the JSON body as the hook's decision; an empty object
            # decides nothing, which is what an observer should do.
            return JSONResponse({})

        return router

    async def _await_human(
        self,
        *,
        conversation: Conversation,
        conversation_address: ChannelAddress,
        run_name: str | None,
        requests: DeferredToolRequests,
    ) -> tuple[DeferredActionBatch, DeferredActionBatchResponse | None]:
        """Present a deferred batch as approval/question cards through the channel,
        then park a future until the human response arrives via
        `try_resolve_live_deferred`. The Claude session stays open in-process while
        this awaits, so the answer is not durable across an Octomate restart.

        Returns `(batch, None)` if the wait exceeds `config.approval_timeout`; the
        batch is marked expired and the caller denies the pending tool so the live
        run unblocks."""
        channel = self.octomate.channels.get(conversation_address.channel_tentacle_id)
        if channel is None:
            raise RuntimeError(
                f"no channel {conversation_address.channel_tentacle_id!r} to "
                f"present a Claude approval/question"
            )
        batch = await channel.feelers.present_actions(
            action_manager=self.octomate.deferred_actions,
            conversation=conversation,
            agent_tentacle_id=self.id,
            run_name=run_name,
            source_address=conversation_address,
            target_address=conversation_address,
            target_mode="sub" if conversation_address.thread_id else "main",
            decision=None,
            requests=requests,
        )
        future: asyncio.Future[DeferredActionBatchResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[batch.id] = future
        try:
            response = await asyncio.wait_for(
                asyncio.shield(future), self.config.approval_timeout
            )
        except asyncio.TimeoutError:
            await self.octomate.deferred_actions.mark_batch(batch.id, "expired")
            return batch, None
        finally:
            self.pending.pop(batch.id, None)
        await self.octomate.deferred_actions.resolve_batch(response)
        return batch, response

    async def __aexit__(self, *exc: object) -> None:
        """Cancel any approvals/questions still awaiting a human so their parked
        runs unblock instead of hanging shutdown. The pending tools are denied as
        the cancellation unwinds; the live sessions are not durable across this."""
        for future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        self.pending.clear()
        # Interrupt any run still streaming so its client/transport tears down
        # instead of orphaning a subprocess or SSH connection at shutdown.
        for client in self.live_clients.values():
            with contextlib.suppress(Exception):
                await client.interrupt()
        self.live_clients.clear()

    async def _iter_events(
        self,
        user_prompt: str | Sequence[UserContent] | None,
        *,
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
        run_name: str | None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        model: Model | KnownModelName | str | None = None,
        instructions: AgentInstructions[None] = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        conversation = await self.octomate.conversations.ensure(
            thread_id,
            agent_tentacle_id=self.id,
        )
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(user_prompt)
        # Tools the user already granted "allow for session" on this conversation
        # auto-approve without a card; new grants extend the set and persist.
        session_allowed: set[str] = set(conversation.allowed_tools)

        if output_type is DeferredToolRequests or (
            isinstance(output_type, (list, tuple))
            and DeferredToolRequests in output_type
        ):
            raise ValueError(
                "ClaudeCodeTentacle does not support DeferredToolRequests "
                "in output_type"
            )

        if output_type is not None:
            output_adapter: TypeAdapter[RunOutputDataT] | None = TypeAdapter(
                output_type
            )
            output_schema: JsonSchemaValue | None = output_adapter.json_schema()
        else:
            output_adapter = None
            output_schema = None
        output_format = (
            {"type": "json_schema", "schema": output_schema}
            if output_schema is not None
            else None
        )
        # Per-run model override (e.g. Sonnet for triage, Opus for reception);
        # the SDK takes a CLI model string, so a pydantic-ai Model yields its name.
        if isinstance(model, Model):
            cli_model = model.model_name
        elif isinstance(model, str):
            cli_model = model
        else:
            cli_model = None

        async def can_use_tool(
            tool_name: str,
            input_data: JsonObject,
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            if tool_name in session_allowed:
                return PermissionResultAllow(updated_input=input_data)
            requests = DeferredToolRequests(
                approvals=[
                    ToolCallPart(
                        tool_name=tool_name,
                        args=input_data,
                        tool_call_id=context.tool_use_id or tool_name,
                    )
                ]
            )
            batch, response = await self._await_human(
                conversation=conversation,
                conversation_address=conversation_address,
                run_name=run_name,
                requests=requests,
            )
            # `can_use_tool` fires per tool call, so we built the batch with a
            # single approval — this is that one action.
            action = next(iter(batch.approvals))
            approved = response is not None and bool(
                response.approvals.get(action.id, False)
            )
            if approved and response is not None and response.allow_session:
                session_allowed.add(tool_name)
                await self.octomate.conversations.grant_session_tool(
                    conversation, tool_name
                )
            if approved:
                return PermissionResultAllow(updated_input=input_data)
            if response is None:
                return PermissionResultDeny(
                    message=f"The approval for {tool_name} expired without a response."
                )
            return PermissionResultDeny(
                message=f"The user declined permission to run {tool_name}."
            )

        async def ask_user_question(
            hook_input: HookInput,
            tool_use_id: str | None,
            context: HookContext,
        ) -> HookJSONOutput:
            # Registered only for PreToolUse/AskUserQuestion, so the input is always
            # a PreToolUseHookInput. `can_use_tool` can only allow/deny, so the
            # answer is fed back by denying with the answer as the reason.
            tool_input = cast(PreToolUseHookInput, hook_input)["tool_input"]
            asked = tool_input.get("questions") or []
            requests = DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="AskUserQuestion",
                        args={
                            "questions": [
                                QuestionRequest(
                                    question=str(item.get("question", "")),
                                    choices=[
                                        str(option.get("label", ""))
                                        for option in item.get("options", [])
                                    ][:MAX_QUESTION_CHOICES]
                                    or None,
                                    hint=str(item.get("header", "")),
                                )
                                for item in asked
                            ]
                        },
                        tool_call_id=tool_use_id or "AskUserQuestion",
                    )
                ]
            )
            batch, response = await self._await_human(
                conversation=conversation,
                conversation_address=conversation_address,
                run_name=run_name,
                requests=requests,
            )
            answered = (
                [
                    f"{question.args['question']}: {response.answers[question.id]}"
                    for question in sorted(batch.questions)
                    if response.answers.get(question.id)
                ]
                if response is not None
                else []
            )
            reason = "\n".join(answered) or "The user did not provide an answer."
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        options = ClaudeAgentOptions(
            cwd=self.config.cwd,
            model=cli_model,
            permission_mode=SDK_PERMISSION_MODE[conversation.permission_mode],
            max_turns=self.config.max_turns,
            resume=conversation.external_id,
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[ask_user_question])
                ]
            },
            output_format=output_format,
            # Stream partial assistant messages so the accumulator can emit token
            # deltas (typewriter) instead of whole blocks; see ClaudeRunAccumulator.
            include_partial_messages=True,
            # Native Claude clients hide sdk-py transcripts from history. Tag these
            # user-routed sessions like CLI runs so they stay visible there too.
            env={"CLAUDE_CODE_ENTRYPOINT": "cli"},
        )
        if isinstance(user_prompt, str):
            prompt_text = user_prompt
        elif user_prompt:
            prompt_text = "\n".join(
                part for part in user_prompt if isinstance(part, str)
            )
        else:
            prompt_text = ""
        if not prompt_text:
            raise ValueError("ClaudeCodeTentacle requires a non-empty text prompt")
        if isinstance(instructions, str):
            prompt_text = "\n\n".join([instructions, prompt_text])

        transport = (
            SSHTransport(prompt_text, options, ssh=self.config.ssh)
            if self.config.ssh is not None
            else None
        )
        with logfire.span(
            "ClaudeCodeTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=run_name or "claude",
            conversation_address=str(conversation_address),
            transport=(
                f"ssh:{self.config.ssh.host}"
                if self.config.ssh is not None
                else "local"
            ),
        ):
            async with ClaudeSDKClient(options=options, transport=transport) as client:
                # One live run per conversation: register this client and interrupt
                # any prior run for the same thread so a mid-run follow-up supersedes
                # it. The weak-value map drops this entry once the run ends and the
                # client is collected — no manual deregistration.
                previous = self.live_clients.get(thread_id)
                self.live_clients[thread_id] = client
                if previous is not None and previous is not client:
                    with contextlib.suppress(Exception):
                        await previous.interrupt()
                await client.query(prompt_text)
                async for message in client.receive_response():
                    for event in accumulator.consume(message):
                        yield event
            run_id = str(uuid7())
            recorded_run = await self.octomate.conversations.record_agent_run(
                conversation,
                run_id=run_id,
                messages=accumulator.messages,
                name=run_name,
                external_id=accumulator.session_id,
            )
            if source_thread_message_ids:
                if recorded_run is None:
                    raise RuntimeError(
                        "prompt-source bindings require a persisted Claude run"
                    )
                prompt_request = next(
                    (
                        message
                        for message in recorded_run.messages
                        if isinstance(message, ModelRequest) and message.role == "user"
                    ),
                    None,
                )
                if prompt_request is None:
                    raise RuntimeError(
                        "prompt-source bindings require a persisted user ModelRequest"
                    )
                source_message_ids = list(source_thread_message_ids)
                await self.octomate.thread_manager.bind_messages(
                    source_message_ids,
                    prompt_request.id,
                    kind="request_source",
                    run_id=recorded_run.id,
                )
                source_thread = await self.octomate.thread_manager.ensure(
                    source_thread_address or conversation_address
                )
                await self.octomate.thread_manager.advance_prompt_cursor(
                    source_thread,
                    source_message_ids[-1],
                )
            if output_adapter is not None and accumulator.structured_output is not None:
                # The model instance rides the str-typed event stream; `run`
                # restores the declared output type at its boundary.
                structured = accumulator.build_structured_result(
                    output_adapter,
                    run_id=run_id,
                    conversation_id=str(conversation.id),
                )
                yield AgentRunResultEvent(
                    cast(
                        "AgentRunResult[str]",
                        structured,
                    )
                )
            else:
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
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
        conversation_address: ChannelAddress,
        thread_id: uuid.UUID | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[uuid.UUID] | None = None,
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
            user_prompt,
            conversation_address=conversation_address,
            thread_id=thread_id,
            source_thread_address=source_thread_address,
            source_thread_message_ids=source_thread_message_ids,
            run_name=run_name,
            output_type=output_type,
            model=model,
            instructions=instructions,
            capabilities=capabilities,
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("Claude run completed without a result")
        # With output_type the event carried a structured AgentRunResult cast to
        # str for the stream; restore the declared output type here.
        if output_type is not None:
            return cast("AgentRunResult[RunOutputDataT]", result)
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
                user_prompt,
                conversation_address=conversation_address,
                thread_id=thread_id,
                source_thread_address=source_thread_address,
                source_thread_message_ids=source_thread_message_ids,
                run_name=run_name,
                output_type=output_type,
                model=model,
                instructions=instructions,
                capabilities=capabilities,
            )
        )
