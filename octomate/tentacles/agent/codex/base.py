from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, cast, overload

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from openai_codex import AsyncCodex, AsyncThread, AsyncTurnHandle
from openai_codex._sandbox import _sandbox_mode
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import (
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    Personality,
    ReasoningEffort,
    ReasoningSummary,
    ReasoningSummaryValue,
    ThreadResumeParams,
    ThreadStartParams,
    TurnStatus,
)
from pydantic import SecretStr, TypeAdapter
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
from pydantic_ai.messages import TextContent, ToolCallPart, UserContent
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from rich.style import Style
from uuid_utils.compat import uuid7

from octomate.capabilities.deferred import DeferredSuspender
from octomate.capabilities.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import CodexApprovalMode, CodexConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
    ConversationPermissionMode,
)
from octomate.schemas.deferred import DeferredActionBatch, QuestionRequest
from octomate.schemas.messages import ModelRequest
from pydantic_ai.settings import ThinkingEffort

from octomate.telemetry import codex_logfire
from octomate.tentacles.agent.base import AgentSpecInput, AgentTentacle
from octomate.tentacles.agent.codex.adapter import (
    CODEX_PROVIDER_NAME,
    CodexRunAccumulator,
    json_object_adapter,
)
from octomate.tentacles.agent.codex.hooks import CODEX_HOOK_PATH, CodexHookInput
from octomate.tentacles.agent.codex.hooks import DRIVEN_ENV
from octomate.tentacles.agent.codex.ingest import CodexHookIngest
from octomate.tentacles.agent.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.agent.hooks import hook_guard
from octomate.tentacles.agent.locks import SessionLocks
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.base import Octomate


# The public openai_codex API can't express two things the `user` approval bridge
# needs: an approval-handler callback on the async client, and the
# `approvals_reviewer=user` thread option. Both are reached through underscored SDK
# internals (`AsyncCodexClient._sync` in `CodexTentacle.__aenter__`, and
# `_client.thread_start` / `_sandbox_mode` in the thread start/resume helpers),
# pinned to openai-codex 0.1.0b3; a Codex upgrade that changes them surfaces there.


@dataclass(frozen=True)
class CodexApprovalPlan:
    """The one mapping from Octomate's approval mode onto the SDK's approval knobs."""

    # Public thread/turn `approval_mode`, or None when only the reviewer path fits.
    sdk_mode: ApprovalMode | None
    # Equivalent raw params for the private `user`-reviewer path.
    policy: AskForApproval
    reviewer: ApprovalsReviewer | None


def codex_approval_plan(approval_mode: CodexApprovalMode) -> CodexApprovalPlan:
    if approval_mode == "auto_review":
        return CodexApprovalPlan(
            sdk_mode=ApprovalMode.auto_review,
            policy=AskForApproval(root=AskForApprovalValue.on_request),
            reviewer=ApprovalsReviewer.auto_review,
        )
    if approval_mode == "deny_all":
        return CodexApprovalPlan(
            sdk_mode=ApprovalMode.deny_all,
            policy=AskForApproval(root=AskForApprovalValue.never),
            reviewer=None,
        )
    return CodexApprovalPlan(
        sdk_mode=None,
        policy=AskForApproval(root=AskForApprovalValue.on_request),
        reviewer=ApprovalsReviewer.user,
    )


@dataclass
class PooledCodexClient:
    client: AsyncCodex
    # The live SDK thread handle for this Octomate thread; created on first turn and
    # reused so later turns continue the same Codex thread on the warm process.
    thread: AsyncThread | None = None
    last_used: float = 0.0
    # Active runs holding this client; a client with `in_use > 0` is never evicted.
    in_use: int = 0


class CodexClientPool:
    """Per-conversation pool of Codex app-server clients.

    Each conversation keeps its own warm Codex process (and `AsyncThread` handle),
    so consecutive turns continue the same Codex thread without relaunching the
    app-server. Keyed by conversation id, not thread id: a thread also holds
    subagent conversations, each of which needs its own client rather than sharing
    (and evicting) the thread's. Idle clients are closed lazily past `idle_ttl`, and an LRU
    `max_clients` cap bounds how many stay warm; a client with a live turn is never
    evicted. Drained entirely on tentacle shutdown."""

    def __init__(
        self,
        *,
        build: Callable[[uuid.UUID], AsyncCodex],
        max_clients: int | None,
        idle_ttl: float | None,
    ) -> None:
        self.build = build
        self.max_clients = max_clients
        self.idle_ttl = idle_ttl
        self.clients: OrderedDict[uuid.UUID, PooledCodexClient] = OrderedDict()
        self.lock = asyncio.Lock()

    async def acquire(self, conversation_id: uuid.UUID) -> PooledCodexClient:
        async with self.lock:
            await self.evict_idle()
            pooled = self.clients.get(conversation_id)
            if pooled is None:
                client = self.build(conversation_id)
                await client.__aenter__()
                pooled = PooledCodexClient(client=client)
                self.clients[conversation_id] = pooled
            self.clients.move_to_end(conversation_id)
            pooled.in_use += 1
            pooled.last_used = time.monotonic()
            await self.evict_over_cap()
            return pooled

    async def release(self, conversation_id: uuid.UUID) -> None:
        async with self.lock:
            pooled = self.clients.get(conversation_id)
            if pooled is not None:
                pooled.in_use = max(0, pooled.in_use - 1)
                pooled.last_used = time.monotonic()

    async def evict_idle(self) -> None:
        if self.idle_ttl is None:
            return
        cutoff = time.monotonic() - self.idle_ttl
        for conversation_id in list(self.clients):
            pooled = self.clients[conversation_id]
            if pooled.in_use == 0 and pooled.last_used < cutoff:
                del self.clients[conversation_id]
                await self.close(pooled)

    async def evict_over_cap(self) -> None:
        if self.max_clients is None:
            return
        # `clients` is LRU-ordered (move_to_end on acquire), so the front is the
        # least-recently-used; skip any client with a live turn.
        for conversation_id in list(self.clients):
            if len(self.clients) <= self.max_clients:
                break
            pooled = self.clients[conversation_id]
            if pooled.in_use == 0:
                del self.clients[conversation_id]
                await self.close(pooled)

    async def aclose(self) -> None:
        async with self.lock:
            pooled_clients = list(self.clients.values())
            self.clients.clear()
        for pooled in pooled_clients:
            await self.close(pooled)

    @staticmethod
    async def close(pooled: PooledCodexClient) -> None:
        # Pair the `__aenter__` done in `acquire` with the SDK's own `__aexit__`
        # teardown, so client init and shutdown stay explicit and matched.
        with contextlib.suppress(Exception):
            await pooled.client.__aexit__(None, None, None)


@dataclass
class CodexBridgeContext:
    loop: asyncio.AbstractEventLoop
    conversation: Conversation
    conversation_address: ChannelAddress
    run_name: str | None
    session_allowed: set[str]
    permission_mode: ConversationPermissionMode


@dataclass
class CodexTentacle(AgentTentacle[str, None]):
    """OpenAI Codex SDK runner exposed as an Octomate agent tentacle.

    Codex owns its conversation state through SDK threads. Octomate stores the
    Codex thread id as the conversation `external_id`, then resumes that thread for
    later turns. Each Octomate thread runs on its own pooled Codex client (an
    app-server process), created on first use and reused across the thread's turns;
    the tentacle itself only owns the pool. The notification stream is translated
    into the same pydantic-ai event and message projections that channel feelers
    already render for other agents.
    """

    config: CodexConfig = field(init=False)
    # Bearer credential its hook router requires of native sessions.
    hook_secret: SecretStr = field(init=False, repr=False)
    pool: CodexClientPool | None = field(default=None, init=False, repr=False)
    live_turns: dict[uuid.UUID, AsyncTurnHandle] = field(
        default_factory=dict, init=False
    )
    bridge_contexts: dict[uuid.UUID, CodexBridgeContext] = field(
        default_factory=dict, init=False
    )

    # Codex approvals/questions are answered in-process through SDK callbacks while
    # the turn stays live. `pending` parks the card response futures.
    in_process: ClassVar[bool] = True

    # OpenAI's own green, so Codex's lines read as Codex's in a console it shares
    # with every other tentacle.
    brand_color: ClassVar[Style | None] = Style(color="#10A37F", bold=True)

    description: str = (
        "Codex coding agent for repository-aware software engineering tasks."
    )

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: CodexConfig,
        hook_secret: SecretStr,
        description: str | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.hook_secret = hook_secret
        self.description = description or self.description
        self.pool = None
        self.live_turns = {}
        self.bridge_contexts = {}
        self.pending = {}
        self.claims = {model: claim for model, claim in config.claims.items()}
        self.models = {model: model for model in config.models}
        self.session_locks = SessionLocks()
        self.session_tailer = CodexTranscriptTailer(
            self.octomate.conversations,
            self.octomate.thread_manager,
            self.session_locks,
        )
        self.session_ingest = CodexHookIngest(
            self.octomate,
            self.session_tailer,
            self.session_locks,
            # Accepted alongside Codex's own tree, never instead of it.
            extra_transcript_roots=(
                (config.transcript_root,) if config.transcript_root else ()
            ),
        )

    def routers(self) -> tuple[APIRouter]:
        return (self.hook_router,)

    @cached_property
    def hook_router(self) -> APIRouter:
        router = APIRouter(
            tags=["codex"], dependencies=[Depends(hook_guard(self.hook_secret))]
        )

        @router.post(CODEX_HOOK_PATH, summary="Codex native-session hook pipe")
        async def receive_hook(event: CodexHookInput) -> JSONResponse:
            await self.session_ingest.handle(event)
            return JSONResponse({})

        return router

    async def __aenter__(self) -> CodexTentacle:
        # Only the pool is tentacle-wide shared state; each conversation's Codex
        # client is built here, entered/exited through the SDK's async context, and
        # reused or evicted by the pool.
        def new_client(conversation_id: uuid.UUID) -> AsyncCodex:
            runtime = replace(
                self.config.runtime,
                env={**(self.config.runtime.env or {}), DRIVEN_ENV: "1"},
            )
            client = AsyncCodex(config=runtime)
            if self.config.approval_mode == "user":
                # One client per conversation, so bind the approval handler to this
                # conversation id — no guessing which live context an SDK request
                # belongs to. The public constructor has no handler hook, so swap in a
                # sync client that has one (SDK-private) before the transport lazily
                # starts.
                def handler(method: str, params: JsonObject | None) -> JsonObject:
                    return self.handle_sdk_request(conversation_id, method, params)

                client._client._sync = CodexClient(
                    config=runtime,
                    approval_handler=handler,
                )
            return client

        self.pool = CodexClientPool(
            build=new_client,
            max_clients=self.config.max_clients,
            idle_ttl=self.config.client_idle_ttl,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.session_tailer.shutdown()
        for turn in list(self.live_turns.values()):
            with contextlib.suppress(Exception):
                await turn.interrupt()
        self.live_turns.clear()
        self.bridge_contexts.clear()
        for future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        self.pending.clear()
        if self.pool is not None:
            await self.pool.aclose()
            self.pool = None

    async def _await_human(
        self,
        *,
        context: CodexBridgeContext,
        requests: DeferredToolRequests,
    ) -> tuple[DeferredActionBatch, DeferredActionBatchResponse | None]:
        channel = self.octomate.channels.get(
            context.conversation_address.channel_tentacle_id
        )
        if channel is None:
            raise RuntimeError(
                "no channel "
                f"{context.conversation_address.channel_tentacle_id!r} to present "
                "a Codex approval/question"
            )
        batch = await channel.feelers.present_actions(
            action_manager=self.octomate.deferred_actions,
            conversation=context.conversation,
            agent_tentacle_id=self.id,
            run_name=context.run_name,
            source_address=context.conversation_address,
            target_address=context.conversation_address,
            target_mode="sub" if context.conversation_address.thread_id else "main",
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

    def handle_sdk_request(
        self, conversation_id: uuid.UUID, method: str, params: JsonObject | None
    ) -> JsonObject:
        # The SDK approval handler fires on the transport thread; bridge each request
        # onto the run's event loop and block that thread until the human answers.
        context = self.bridge_contexts.get(conversation_id)
        if context is None:
            return self.deny_sdk_request(f"Octomate has no live context for {method}.")
        if self.is_approval_request(method):
            future = asyncio.run_coroutine_threadsafe(
                self.answer_sdk_approval_request(
                    context=context, method=method, params=params
                ),
                context.loop,
            )
            try:
                return future.result()
            except Exception as exc:
                return self.deny_sdk_request(str(exc))
        if self.is_question_request(method):
            future = asyncio.run_coroutine_threadsafe(
                self.answer_sdk_question_request(
                    context=context, method=method, params=params
                ),
                context.loop,
            )
            try:
                return future.result()
            except Exception as exc:
                return {"action": "decline", "message": str(exc)}
        return {}

    async def answer_sdk_approval_request(
        self,
        *,
        context: CodexBridgeContext,
        method: str,
        params: JsonObject | None,
    ) -> JsonObject:
        args = params or {}
        tool_name = self.approval_tool_name(method)
        if self.approval_is_allowed(context, tool_name):
            return {"decision": "accept"}
        tool_call_id = self.sdk_request_id(method, args)
        requests = DeferredToolRequests(
            approvals=[
                ToolCallPart(
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=tool_call_id,
                    provider_name=CODEX_PROVIDER_NAME,
                )
            ]
        )
        batch, response = await self._await_human(
            context=context,
            requests=requests,
        )
        action = next(iter(batch.approvals))
        approved = response is not None and bool(
            response.approvals.get(action.id, False)
        )
        if approved and response is not None and response.allow_session:
            context.session_allowed.add(tool_name)
            await self.octomate.conversations.grant_session_tool(
                context.conversation,
                tool_name,
            )
        if approved:
            return {"decision": "accept"}
        if response is None:
            return self.deny_sdk_request(
                f"The approval for {tool_name} expired without a response."
            )
        return self.deny_sdk_request(
            f"The user declined permission to run {tool_name}."
        )

    async def answer_sdk_question_request(
        self,
        *,
        context: CodexBridgeContext,
        method: str,
        params: JsonObject | None,
    ) -> JsonObject:
        args = params or {}
        tool_call_id = self.sdk_request_id(method, args)
        question = self.question_from_sdk_request(method, args)
        requests = DeferredToolRequests(
            calls=[
                ToolCallPart(
                    tool_name="codex_user_input",
                    args={"questions": [question]},
                    tool_call_id=tool_call_id,
                    provider_name=CODEX_PROVIDER_NAME,
                )
            ]
        )
        batch, response = await self._await_human(
            context=context,
            requests=requests,
        )
        if response is None:
            return {"action": "decline", "message": "The user did not answer."}
        answers = [
            response.answers.get(action.id)
            for action in sorted(batch.questions)
            if response.answers.get(action.id)
        ]
        answer = "\n".join(str(item) for item in answers if item)
        if not answer:
            return {"action": "decline", "message": "The user did not answer."}
        content_key = self.question_content_key(args)
        return {"action": "accept", "content": {content_key: answer}, "answer": answer}

    @staticmethod
    def sdk_request_id(method: str, params: JsonObject) -> str:
        for key in ("itemId", "requestId", "id", "callId"):
            value = params.get(key)
            if isinstance(value, str):
                return value
        return method

    @staticmethod
    def deny_sdk_request(message: str) -> JsonObject:
        return {"decision": "deny", "message": message}

    @staticmethod
    def is_approval_request(method: str) -> bool:
        return method.endswith("/requestApproval") or "requestApproval" in method

    @staticmethod
    def approval_tool_name(method: str) -> str:
        if method == "item/commandExecution/requestApproval":
            return "codex_command_execution"
        if method == "item/fileChange/requestApproval":
            return "codex_file_change"
        stem = method.removesuffix("/requestApproval")
        sanitized = "".join(
            character if character.isalnum() else "_" for character in stem
        ).strip("_")
        return f"codex_{sanitized or 'approval'}"

    @staticmethod
    def approval_is_allowed(context: CodexBridgeContext, tool_name: str) -> bool:
        if context.permission_mode == "bypass_permissions":
            return True
        if (
            context.permission_mode == "accept_edits"
            and tool_name == "codex_file_change"
        ):
            return True
        return tool_name in context.session_allowed

    @staticmethod
    def is_question_request(method: str) -> bool:
        lowered = method.lower()
        return "elicitation" in lowered or "question" in lowered

    @staticmethod
    def question_from_sdk_request(method: str, params: JsonObject) -> QuestionRequest:
        question = params.get("message") or params.get("question") or params.get("prompt")
        if not isinstance(question, str) or not question.strip():
            question = f"Codex is requesting input for {method}."
        return QuestionRequest(question=question)

    @staticmethod
    def question_content_key(params: JsonObject) -> str:
        schema = params.get("requestedSchema")
        if not isinstance(schema, dict):
            return "answer"
        properties = schema.get("properties")
        if not isinstance(properties, dict) or len(properties) != 1:
            return "answer"
        key = next(iter(properties))
        return key if isinstance(key, str) and key else "answer"

    async def start_codex_thread(
        self,
        client: AsyncCodex,
        *,
        approval_mode: CodexApprovalMode,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> AsyncThread:
        plan = codex_approval_plan(approval_mode)
        if plan.sdk_mode is not None:
            return await client.thread_start(
                approval_mode=plan.sdk_mode,
                base_instructions=base_instructions,
                cwd=cwd,
                developer_instructions=developer_instructions,
                ephemeral=ephemeral,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=sandbox,
            )
        # `user` reviewer has no public thread_start knob; go through the raw params.
        await client._ensure_initialized()
        started = await client._client.thread_start(
            ThreadStartParams(
                approval_policy=plan.policy,
                approvals_reviewer=plan.reviewer,
                base_instructions=base_instructions,
                cwd=cwd,
                developer_instructions=developer_instructions,
                ephemeral=ephemeral,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=_sandbox_mode(sandbox),
            )
        )
        return AsyncThread(client, started.thread.id)

    async def resume_codex_thread(
        self,
        client: AsyncCodex,
        *,
        thread_id: str,
        approval_mode: CodexApprovalMode,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> AsyncThread:
        plan = codex_approval_plan(approval_mode)
        if plan.sdk_mode is not None:
            return await client.thread_resume(
                thread_id,
                approval_mode=plan.sdk_mode,
                base_instructions=base_instructions,
                cwd=cwd,
                developer_instructions=developer_instructions,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=sandbox,
            )
        await client._ensure_initialized()
        resumed = await client._client.thread_resume(
            thread_id,
            ThreadResumeParams(
                thread_id=thread_id,
                approval_policy=plan.policy,
                approvals_reviewer=plan.reviewer,
                base_instructions=base_instructions,
                cwd=cwd,
                developer_instructions=developer_instructions,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=_sandbox_mode(sandbox),
            ),
        )
        return AsyncThread(client, resumed.thread.id)

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
        effort: ThinkingEffort | None = None,
        instructions: AgentInstructions[None] = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        if self.pool is None:
            raise RuntimeError("CodexTentacle.run requires the tentacle to be entered")
        if output_type is DeferredToolRequests or (
            isinstance(output_type, (list, tuple))
            and DeferredToolRequests in output_type
        ):
            raise ValueError(
                "CodexTentacle does not support DeferredToolRequests in output_type"
            )

        conversation = await self.octomate.conversations.ensure(
            thread_id,
            agent_tentacle_id=self.id,
        )
        accumulator = CodexRunAccumulator()
        accumulator.begin(user_prompt)

        if output_type is not None:
            output_adapter: TypeAdapter[RunOutputDataT] | None = TypeAdapter(
                output_type
            )
            output_schema = json_object_adapter.validate_python(
                output_adapter.json_schema()
            )
        else:
            output_adapter = None
            output_schema: JsonObject | None = None

        if isinstance(model, Model):
            sdk_model = model.model_name
        elif isinstance(model, str):
            sdk_model = model
        else:
            sdk_model = None

        if isinstance(user_prompt, str):
            prompt_text = user_prompt
        elif user_prompt:
            prompt_text = "\n".join(
                part.content if isinstance(part, TextContent) else part
                for part in user_prompt
                if isinstance(part, str | TextContent)
            )
        else:
            prompt_text = ""
        if not prompt_text:
            raise ValueError("CodexTentacle requires a non-empty text prompt")
        if isinstance(instructions, str):
            prompt_text = "\n\n".join([instructions, prompt_text])

        approval_plan = codex_approval_plan(self.config.approval_mode)
        sandbox = Sandbox[self.config.sandbox]
        personality = (
            Personality(self.config.personality)
            if self.config.personality is not None
            else None
        )
        # The caller's normalized effort wins over the config default; both speak
        # values the SDK scale already contains, so no mapping table is needed.
        turn_effort = (
            ReasoningEffort(effort or self.config.effort)
            if effort is not None or self.config.effort is not None
            else None
        )
        if self.config.summary is None:
            summary: ReasoningSummary | None = None
        elif self.config.summary == "none":
            summary = ReasoningSummary("none")
        else:
            summary = ReasoningSummary(ReasoningSummaryValue(self.config.summary))

        codex_thread_id: str | None = None
        with codex_logfire.span(
            "CodexTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=run_name or "codex",
            conversation_address=str(conversation_address),
        ):
            pooled = await self.pool.acquire(conversation.id)
            try:
                codex_thread = pooled.thread
                if codex_thread is None:
                    if conversation.external_id:
                        codex_thread = await self.resume_codex_thread(
                            pooled.client,
                            thread_id=conversation.external_id,
                            approval_mode=self.config.approval_mode,
                            base_instructions=self.config.base_instructions,
                            cwd=self.config.cwd,
                            developer_instructions=self.config.developer_instructions,
                            model=sdk_model,
                            model_provider=self.config.model_provider,
                            personality=personality,
                            sandbox=sandbox,
                        )
                    else:
                        codex_thread = await self.start_codex_thread(
                            pooled.client,
                            approval_mode=self.config.approval_mode,
                            base_instructions=self.config.base_instructions,
                            cwd=self.config.cwd,
                            developer_instructions=self.config.developer_instructions,
                            ephemeral=self.config.ephemeral,
                            model=sdk_model,
                            model_provider=self.config.model_provider,
                            personality=personality,
                            sandbox=sandbox,
                        )
                    pooled.thread = codex_thread
                codex_thread_id = codex_thread.id
                self.bridge_contexts[conversation.id] = CodexBridgeContext(
                    loop=asyncio.get_running_loop(),
                    conversation=conversation,
                    conversation_address=conversation_address,
                    run_name=run_name,
                    session_allowed=set(conversation.allowed_tools),
                    permission_mode=conversation.permission_mode,
                )
                try:
                    with self.session_ingest.driving(codex_thread.id):
                        turn = await codex_thread.turn(
                            prompt_text,
                            approval_mode=approval_plan.sdk_mode,
                            cwd=self.config.cwd,
                            effort=turn_effort,
                            model=sdk_model,
                            output_schema=output_schema,
                            personality=personality,
                            sandbox=sandbox,
                            summary=summary,
                        )
                        previous = self.live_turns.get(conversation.id)
                        self.live_turns[conversation.id] = turn
                        if previous is not None and previous is not turn:
                            with contextlib.suppress(Exception):
                                await previous.interrupt()
                        try:
                            async for notification in turn.stream():
                                for event in accumulator.consume(notification):
                                    yield event
                        finally:
                            if self.live_turns.get(conversation.id) is turn:
                                self.live_turns.pop(conversation.id, None)
                finally:
                    self.bridge_contexts.pop(conversation.id, None)
            finally:
                await self.pool.release(conversation.id)

        run_id = str(uuid7())
        recorded_run = await self.octomate.conversations.record_agent_run(
            conversation,
            run_id=run_id,
            messages=accumulator.messages,
            name=run_name,
            external_id=accumulator.thread_id or codex_thread_id,
        )
        if source_thread_message_ids:
            if recorded_run is None:
                raise RuntimeError("prompt-source bindings require a persisted Codex run")
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
        if accumulator.turn_status == TurnStatus.failed:
            raise AgentRunError(accumulator.turn_error or "Codex turn failed")
        if output_adapter is not None:
            structured = accumulator.build_structured_result(
                output_adapter,
                run_id=run_id,
                conversation_id=str(conversation.id),
            )
            yield AgentRunResultEvent(cast("AgentRunResult[str]", structured))
        else:
            yield AgentRunResultEvent(
                accumulator.build_result(
                    run_id=run_id,
                    conversation_id=str(conversation.id),
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
            effort=effort,
            instructions=instructions,
            capabilities=capabilities,
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("Codex run completed without a result")
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
        effort: ThinkingEffort | None = None,
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
                effort=effort,
                instructions=instructions,
                capabilities=capabilities,
            )
        )
