"""The DeepSeek Harness (dsh) agent tentacle.

Driven runs go to an owned `dsh web` child over its `/api` gateway, with approvals
and questions bridged to a human; native sessions arrive through the hook and
stream routes mounted here, into `DeepseekHookIngest` and `DeepseekEventTailer`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from functools import cached_property, partial
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar, overload
from uuid import uuid4

import anyio
import httpx
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from octomate_protocol.deepseek import (
    ErrResult,
    OkResult,
    RpcError,
    RpcResult,
)
from octomate_protocol.stream import (
    SESSION_FILE,
    STREAM_PROTOCOL,
    StreamEof,
    StreamFinalize,
    StreamHello,
    StreamLine,
    StreamWelcome,
    client_message_adapter,
)
from pydantic import UUID7, HttpUrl, ValidationError
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

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.events import ActionBatchEvent, MessageSentEvent
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import Claim, DeepseekConfig
from octomate.prompts import tagged
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandOutcome,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    DeferredActionBatch,
    QuestionRequest,
)
from octomate.schemas.messages import ModelRequest
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import DEEPSEEK_NATIVE_ID
from octomate.schemas.user import UserProfile
from octomate.telemetry import agent_input_message_attributes, deepseek_logfire
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle
from octomate.tentacles.deepseek.adapter import (
    DEEPSEEK_PROVIDER_NAME,
    DeepseekRunAccumulator,
)
from octomate.tentacles.deepseek.catalog import DeepseekCommandDescriptor
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.hooks import DeepseekHookInput
from octomate.tentacles.deepseek.ingest import DeepseekHookIngest
from octomate.tentacles.deepseek.ink import DeepseekInk, TurnFrame
from octomate.tentacles.deepseek.process import DeepseekProcess
from octomate.tentacles.deepseek.tailer import DeepseekEventTailer
from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    CommandExecutionValue,
    ModelCatalog,
    PermissionCatalog,
    PermissionPresetData,
    QuestionRequestedFrame,
    SessionAssistantFrame,
    SessionCreateValue,
    SessionProjectionsValue,
    SessionPromptValue,
    StreamErrorFrame,
    text_of,
    user_message_of,
)
from octomate.tentacles.hooks import hook_guard, hook_sender
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject, JsonValue
from octomate.utils import drain_task

if TYPE_CHECKING:
    from octomate.base import Octomate
    from octomate.managers.conversation import ConversationManager
    from octomate.managers.deferred import DeferredActionManager
    from octomate.managers.user import UserManager
    from octomate.managers.workspaces import WorkspaceManager
    from octomate.mcp.base import KnownBearers

logger = logging.getLogger(__name__)


@dataclass
class DeepseekBridgeContext:
    """The driven turn a dsh approval or question is answered for.

    Held per session while the turn runs; `interactive` is whether a human exists to
    ask at all.
    """

    conversation: Conversation
    session_allowed: set[str]
    interactive: bool
    # What a request pauses the turn on, and the turn's own frames, where its
    # batch joins the stream.
    suspender: DeferredSuspender | None
    frames: asyncio.Queue[TurnFrame]


@dataclass
class DeepseekTentacle(AgentTentacle[str, None]):
    """WIP DeepSeek Harness (dsh) exposed as an Octomate agent tentacle.

    Owns a `dsh web` child with private extension configuration and shared
    native settings and session data. Existing runtimes are never attached. The
    tentacle drives the harness over the `/api` gateway — HTTP POSTs for unary
    calls, the Remote mux WebSocket for events, `POST /api/$events/result` for
    answering the approvals and questions dsh pushes mid-turn. dsh owns its
    conversation state through sessions: octomate stores the dsh session id as
    the conversation `external_id` and prompts the same session for later
    turns. The event
    stream is translated into the same pydantic-ai event and message
    projections channel feelers already render for other agents.

    Native sessions — ones a person drives in dsh's own web UI, CLI, or
    another gateway client — are ingested too: the hook router takes the
    events dsh's `dsh-hooks-claude-code` bridge POSTs in, and the stream
    endpoint takes each session's history entries from the client-side tail
    (`octomate deepseek tail`, reading *its* machine's dsh gateway) for the
    tailer to assemble into turns. Sessions this tentacle is driving are excluded
    from native ingest because their runs are already recorded here.
    """

    # TODO: Implement fork_session when DSH supports a destination cwd.
    # Until then, inherit the base rejection to keep teleport/fork blocked.

    config: DeepseekConfig = field(init=False)
    default_provider: str | None = field(init=False)
    process: DeepseekProcess | None = field(default=None, init=False, repr=False)
    ink: DeepseekInk = field(init=False, repr=False)
    conversations: ConversationManager = field(init=False, repr=False)
    workspaces: WorkspaceManager = field(init=False, repr=False)
    deferred_actions: DeferredActionManager = field(init=False, repr=False)
    users: UserManager = field(init=False, repr=False)
    bearers: KnownBearers = field(init=False, repr=False)
    bridge_contexts: dict[str, DeepseekBridgeContext] = field(
        default_factory=dict, init=False
    )

    # dsh approvals/questions are answered in-process through mux frames while
    # the turn stays live. `pending` parks the card response futures.
    in_process: ClassVar[bool] = True

    native_id: ClassVar[str] = DEEPSEEK_NATIVE_ID

    @property
    def default_permission_mode(self) -> str | None:
        return self.config.permission_mode

    async def apply_permission_mode(
        self, conversation: Conversation, mode: str
    ) -> None:
        session_id = conversation.external_id
        if session_id is not None and session_id in self.driven_sessions:
            await self.ink.set_permission_mode(session_id, mode)

    # DeepSeek's own blue, so dsh's lines read as dsh's in a shared console.
    brand_color: ClassVar[Style | None] = Style(color="#4D6BFE", bold=True)

    description: str = "WIP DeepSeek Harness coding agent for repository-aware software engineering tasks."

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: DeepseekConfig,
        description: str | None = None,
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
        )
        self.config = config
        self.conversations = octomate.conversations
        self.workspaces = octomate.workspaces
        self.deferred_actions = octomate.deferred_actions
        self.users = octomate.users
        self.bearers = octomate.bearers
        self.description = description or self.description
        self.process = None
        # The endpoint is fixed by config, so the client lives as long as the
        # tentacle — launch, runs and teardown all speak through it.
        endpoint = HttpUrl(f"http://{config.host}:{config.port}")
        self.ink = DeepseekInk(
            DeepseekApiClient(
                base_url=endpoint,
                http_client=httpx.AsyncClient(base_url=str(endpoint)),
            )
        )
        self.bridge_contexts = {}
        self.pendings = {}
        self.claims = dict(config.claims)
        self.gateway = config.gateway
        self.models = {}
        self.default_provider = None
        # Serializes turns per conversation: dsh queues a second prompt into a
        # live turn as steering, which would interleave two runs' frames.
        self.conversation_locks = SessionLocks()
        self.session_locks = SessionLocks()
        self.session_tailer = DeepseekEventTailer(
            self.conversations,
            self.threads,
            self.projects,
            self.session_locks,
        )
        self.session_ingest = DeepseekHookIngest(
            self.octomate,
            self.session_tailer,
            self.session_locks,
        )

    def routers(self) -> tuple[APIRouter]:
        return (self.hook_router,)

    @cached_property
    def hook_router(self) -> APIRouter:
        """The hook pipe native dsh sessions POST their events into, and the
        stream endpoint every session's client-side tail feeds history entries
        through (`octomate deepseek tail`) — a tail that reads its machine's
        dsh gateway, not a file, since the log is zstd-framed and the gateway
        serves it decoded. The server never speaks to a client machine's dsh.
        The guard covers the websocket too: FastAPI runs router dependencies
        at the handshake, so a bad bearer is denied with the same 401 before
        any socket opens."""
        verifier = hook_guard(self.bearers)
        resolve_sender = hook_sender(self.users, self.native_id, verifier)
        router = APIRouter(tags=["deepseek"], dependencies=[Depends(verifier)])

        @router.post("/hooks/deepseek", summary="dsh native-session hook pipe")
        async def receive_hook(event: DeepseekHookInput) -> JSONResponse:
            # No principal needed: dsh's hook dialect writes no ledger rows —
            # every durable row is the stream's, attributed at its handshake.
            await self.session_ingest.handle(event)
            return JSONResponse({})

        @router.websocket("/hooks/deepseek/stream")
        async def stream(
            websocket: WebSocket,
            # `param: T = Depends(dep)` is FastAPI's own dependency contract;
            # ruff's B008 exemption misses it when T is a custom class (it is
            # fine with `str`), so the rule bends rather than the checked type.
            sender: UserProfile = Depends(resolve_sender),  # noqa: B008
        ) -> None:
            await self.stream_session(websocket, sender)

        return router

    async def stream_session(self, websocket: WebSocket, sender: UserProfile) -> None:
        """Validate the remote tail's protocol and attach it as an external session.

        `sender` is the verified bearer's profile, resolved at the handshake,
        whose ledger this stream writes.
        """
        await websocket.accept()
        try:
            hello = client_message_adapter.validate_json(await websocket.receive_text())
        except ValidationError:
            await websocket.close(code=1008, reason="expected a hello message")
            return
        except WebSocketDisconnect:
            return
        if not isinstance(hello, StreamHello):
            await websocket.close(code=1008, reason="expected a hello message")
            return
        if hello.protocol != STREAM_PROTOCOL:
            await websocket.close(
                code=1008,
                reason=f"protocol {hello.protocol} unsupported; server speaks "
                f"{STREAM_PROTOCOL}",
            )
            return
        async with self.driving(hello.session_id, native=True):
            await self.stream_attached(websocket, hello, sender)

    async def stream_attached(
        self, websocket: WebSocket, hello: StreamHello, sender: UserProfile
    ) -> None:
        """The attached half of a stream connection: register the session,
        answer the resume seq, then feed each framed entry through the
        tailer's assembly. Offsets are event seqs (`end` is `seq + 1`), and
        the contiguity check works unchanged in that space — dsh seqs are
        dense per session. dsh turns close on their own `turn/end` lines, so
        nothing commits at the boundary either way; a `Stop` on the hook pipe
        reaches here as the state's `stop_event` (`stop_turn`, once the
        stopped turn is durable or its wait ran out), and the relayed
        `finalize` asks the client for its final drain and `eof`."""
        state, offsets = await self.session_tailer.attach_remote(
            hello.session_id, Path(hello.transcript_path), hello.cwd, sender
        )
        logger.info(
            "session %s: remote tail connected (octomate %s)",
            hello.session_id,
            hello.client_version or "unversioned",
        )
        await websocket.send_text(StreamWelcome(offsets=offsets).model_dump_json())

        async def relay_finalize() -> None:
            await state.stop_event.wait()
            try:
                await websocket.send_text(StreamFinalize().model_dump_json())
            except Exception:
                logger.debug(
                    "session %s: finalize relay lost its socket", hello.session_id
                )

        relay = asyncio.create_task(relay_finalize())
        expected = dict(offsets)
        clean = False
        try:
            while True:
                message = client_message_adapter.validate_json(
                    await websocket.receive_text()
                )
                if isinstance(message, StreamEof):
                    clean = True
                    return
                if isinstance(message, StreamHello):
                    await websocket.close(code=1008, reason="hello already received")
                    return
                if not isinstance(message, StreamLine):
                    await websocket.close(
                        code=1008, reason="expected a transcript line"
                    )
                    return
                if message.agent_id is not None:
                    # A dsh session streams as one event sequence; there are no
                    # sibling files to label.
                    await websocket.close(
                        code=1008, reason="deepseek streams a single sequence"
                    )
                    return
                want = expected.get(SESSION_FILE, 0)
                if message.start != want:
                    await websocket.close(
                        code=4000,
                        reason=f"seq gap: expected {want}, got {message.start}",
                    )
                    return
                expected[SESSION_FILE] = message.end
                await self.session_tailer.feed_remote(
                    state, None, message.line, message.start, message.end
                )
        except WebSocketDisconnect:
            pass
        except ValidationError:
            await websocket.close(code=1008, reason="unparseable stream message")
        except Exception:
            logger.exception(
                "session %s: remote tail errored; its open turn is left for the "
                "next connect to re-stream",
                hello.session_id,
            )
        finally:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay
            self.session_tailer.detach_remote(state)
            if clean:
                with contextlib.suppress(Exception):
                    await websocket.close()

    async def start_process(self) -> DeepseekProcess:
        """Start our isolated child and verify its authenticated Remote API."""
        process = DeepseekProcess(
            executable=self.config.executable,
            port=self.config.port,
            extra_args=list(self.config.extra_args),
            dsh_home=self.config.dsh_home,
            ready_timeout=self.config.ready_timeout,
            browser_url=self.config.browser_url,
        )
        base_url = await process.start()
        try:
            if process.launch_token is not None:
                await self.ink.client.authenticate(process.launch_token)
            if not await self.ink.client.answering():
                raise RuntimeError(
                    f"dsh reported {base_url} but does not answer at {self.ink.client.base_url}"
                )
        except BaseException:
            await process.stop()
            raise
        logger.info("dsh Remote API connected")
        return process

    async def probe_commands(self, context: CommandContext) -> CommandCatalog:
        """Read the live session's registry without creating a session or turn."""
        conversation = context.conversation
        if conversation is None or not conversation.external_id:
            return CommandCatalog(
                context=context,
                status="unavailable",
                message="DSH command discovery requires an existing native session.",
            )
        if not self.ink.running:
            return CommandCatalog(
                context=context,
                status="unavailable",
                message="The DSH Remote connection is not running.",
            )
        return CommandCatalog(
            context=context,
            status="ready",
            descriptors={
                entry.model_copy(
                    update={
                        "accepts_attachments": False,
                        "unavailable_reason": "Download session logs in the DSH web UI."
                        if entry.definition_id == "@deepseek-ai/dsh-session-log-export"
                        else entry.unavailable_reason,
                    }
                )
                for entry in await self.ink.list_commands(conversation.external_id)
            },
        )

    async def execute_command(
        self,
        context: CommandContext,
        invocation: CommandInvocation,
        *,
        deferred_suspender: DeferredSuspender | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AsyncGenerator[CommandOutcome | ReactStreamEvent[str], None]:
        """Own direct feedback and any immediate native turn in one invocation."""
        conversation = context.conversation
        if conversation is None or not conversation.external_id:
            yield CommandError(
                status="unavailable",
                message="Start a DSH session before running commands.",
            )
            return
        if invocation.attachments:
            yield CommandError(
                status="unsupported",
                message="DSH command attachments are not supported yet.",
            )
            return
        catalog = await self.discover_commands(context)
        descriptor = {entry.id: entry for entry in catalog.descriptors}.get(
            invocation.command_id
        )
        if not isinstance(descriptor, DeepseekCommandDescriptor):
            yield CommandError(
                status="stale", message="This command changed; refresh commands."
            )
            return
        if not self.ink.running:
            yield CommandError(
                status="unavailable",
                message="The DSH Remote connection is not running.",
            )
            return
        session_id = conversation.external_id
        line = f"/{descriptor.name}"
        if invocation.arguments:
            line += f" {invocation.arguments}"
        project = await self.run_project(conversation.thread_id)
        async with (
            self.conversation_locks.hold(str(conversation.id)),
            self.workspaces.open(conversation.thread_id, project),
            self.driving(session_id),
            contextlib.AsyncExitStack() as resources,
        ):
            conversation = await self.conversations.get(
                conversation.id, with_history=False
            )
            effort = await self.resolve_effort(conversation, model=context.model)
            permission_mode = (
                conversation.permission_mode or self.config.permission_mode
            )
            self.check_permission_mode(permission_mode)
            if context.model is not None:
                await self.ink.select_model(
                    session_id,
                    context.model,
                    default_provider=self.default_provider,
                    reasoning_effort=effort,
                )
            await self.ink.set_permission_mode(session_id, permission_mode)
            queue = await self.ink.subscribe(session_id)
            resources.push_async_callback(self.ink.unsubscribe, session_id)
            self.bridge_contexts[session_id] = DeepseekBridgeContext(
                conversation=conversation,
                session_allowed=set(conversation.allowed_tools),
                interactive=True,
                suspender=deferred_suspender,
                frames=queue,
            )
            resources.callback(self.bridge_contexts.pop, session_id, None)
            execution = await self.ink.execute_command(session_id, line)
            if execution is None:
                yield CommandError(
                    status="stale",
                    message="DSH no longer recognizes this command; refresh commands.",
                )
                return
            async with contextlib.aclosing(
                self.command_events(context, execution, queue)
            ) as events:
                async for event in events:
                    yield event

    async def command_events(
        self,
        context: CommandContext,
        execution: CommandExecutionValue,
        queue: asyncio.Queue[TurnFrame],
    ) -> AsyncGenerator[CommandOutcome | ReactStreamEvent[str], None]:
        """Fence direct results by command ID, and record only an actual native turn.

        Immediate producers such as plan open their turn before command/done.
        Waiting for that record also drains events buffered behind the HTTP reply.
        Goal continuations need a longer lifecycle and are excluded from discovery.
        """
        conversation = context.conversation
        assert conversation is not None
        accumulator = DeepseekRunAccumulator()
        entered = False
        settled = False
        run_id = str(uuid7())
        while not settled or (accumulator.turn_started and not accumulator.turn_ended):
            frame = await queue.get()
            if isinstance(frame, ActionBatchEvent):
                yield frame
                continue
            if isinstance(frame, StreamErrorFrame):
                accumulator.turn_error = frame.error.message
                break
            if isinstance(frame, SessionAssistantFrame):
                events = accumulator.consume_assistant_stream(frame)
            else:
                data = frame.event.data
                if (
                    isinstance(data, dict)
                    and data.get("commandId") == execution.command_id
                ):
                    entered = entered or frame.event.type == "command/run"
                    settled = settled or frame.event.type == "command/done"
                if not entered:
                    continue
                if frame.event.type == "permission/preset":
                    await self.conversations.set_permission_mode(
                        conversation,
                        PermissionPresetData.model_validate(frame.event.data).preset,
                    )
                if (
                    frame.event.type == "user/message"
                    and (message := user_message_of(frame.event)) is not None
                ):
                    accumulator.begin(text_of(message.content))
                if (
                    frame.event.type == "turn/start"
                    and execution.result.text is not None
                ):
                    yield MessageSentEvent(
                        segments=[TextSegment(data={"text": execution.result.text})]
                    )
                events = accumulator.consume(frame)
            for event in events:
                yield event
        if accumulator.turn_started:
            await self.conversations.record_agent_run(
                conversation,
                run_id=run_id,
                messages=accumulator.messages,
                name="command",
                model_name=accumulator.route.model
                if accumulator.route is not None
                else None,
                permission_mode=conversation.permission_mode or context.permission_mode,
                cwd=context.cwd,
                external_id=conversation.external_id,
                native_id=DEEPSEEK_NATIVE_ID,
                native_turn_id=(
                    f"{conversation.external_id}:{accumulator.turn_number}"
                    if accumulator.turn_number is not None
                    else None
                ),
            )
            if accumulator.turn_error or execution.result.kind == "error":
                raise AgentRunError(
                    accumulator.turn_error
                    or execution.result.text
                    or "DSH command failed."
                )
            yield AgentRunResultEvent(
                accumulator.build_result(
                    run_id=run_id, conversation_id=str(conversation.id)
                )
            )
            return
        if accumulator.turn_error or execution.result.kind == "error":
            yield CommandError(
                status="failed",
                message=accumulator.turn_error
                or execution.result.text
                or "DSH command failed.",
            )
            return
        yield CommandResult(
            segments=[TextSegment(data={"text": execution.result.text})]
            if execution.result.text is not None
            else []
        )

    async def discover_models(self) -> None:
        catalog = ModelCatalog.model_validate(
            self.unwrap(
                await self.ink.client.remote("session/modelCatalog", {}),
                "session/modelCatalog",
            )
        )
        for failure in catalog.failures:
            logger.warning(
                "dsh provider %s model discovery failed: %s",
                failure.id,
                failure.message,
            )
        models: dict[str, Model | str] = {}
        claims: dict[str, Claim] = {}
        for provider in catalog.groups:
            for model in provider.models:
                key = f"{provider.id}:{model.id}"
                configured = self.config.claims.get(key)
                efforts = (
                    tuple(effort.id for effort in model.reasoning.efforts)
                    if model.reasoning is not None
                    else configured.efforts
                    if configured is not None
                    else ()
                )
                models[key] = key
                claims[key] = Claim(
                    model.description
                    or (configured.ability if configured else model.name),
                    efforts,
                )
        if not models:
            raise ValueError("DeepSeek Harness advertised no available models")
        self.set_model_catalog(models, claims)
        self.default_model = f"{catalog.default.provider}:{catalog.default.model}"
        self.default_provider = catalog.default.provider

    async def discover_permissions(self) -> None:
        catalog = PermissionCatalog.model_validate(
            self.unwrap(
                await self.ink.client.remote("permissionPresets/catalog", {}),
                "permissionPresets/catalog",
            )
        )
        self.permission_modes = catalog.options
        self.check_permission_mode(self.config.permission_mode)

    async def __aenter__(self) -> DeepseekTentacle:
        await self.ink.__aenter__()
        # start_process leaves the client verified (settings/describe answered);
        # the mux socket must then be open before anything prompts, so a run's
        # first frames cannot outrun the subscribed baseline. A failed
        # handshake is a broken harness, not weather: fail the start rather
        # than retrying.
        try:
            self.process = await self.start_process()
            await self.discover_models()
            await self.discover_permissions()
            await self.ink.start(
                answer_interaction=self.answer_interaction,
                invalidate_commands=partial(self.commands.invalidate, agent_id=self.id),
            )
        except BaseException:
            await self.ink.__aexit__()
            if self.process is not None:
                await self.process.stop()
            self.process = None
            raise
        self.commands.invalidate(agent_id=self.id)
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await super().__aexit__(exc_type, exc_value, traceback)
        self.commands.invalidate(agent_id=self.id)
        with anyio.CancelScope(shield=True):
            cancelled = await drain_task(asyncio.gather(*self.run_tasks))
            self.session_ingest.shutdown()
            await self.session_tailer.shutdown()
            await self.ink.__aexit__(exc_type, exc_value, traceback)
            for future in list(self.pendings.values()):
                if not future.done():
                    future.cancel()
            self.pendings.clear()
            self.bridge_contexts.clear()
            if self.process is not None:
                await self.process.stop()
                self.process = None
        if cancelled:
            raise asyncio.CancelledError

    async def answer_interaction(
        self, frame: ApprovalRequestedFrame | QuestionRequestedFrame
    ) -> RpcResult | None:
        context = self.bridge_contexts.get(frame.session_id)
        if context is None:
            return None
        try:
            if isinstance(frame, ApprovalRequestedFrame):
                return await self.answer_approval(context, frame)
            return await self.answer_questions(context, frame)
        except Exception as error:
            logger.exception(
                "session %s: answering a dsh %s failed",
                frame.session_id,
                frame.type,
            )
            return ErrResult(error=RpcError(code="cancelled", message=str(error)))

    async def answer_approval(
        self, context: DeepseekBridgeContext, frame: ApprovalRequestedFrame
    ) -> RpcResult:
        if not context.interactive:
            # A commissioned run has no human, and dsh's ask-vs-never lives
            # inside the preset rather than in a swappable posture, so the
            # non-interactive contract is declining at the bridge.
            return OkResult(value="rejected")
        if frame.tool_name in context.session_allowed:
            return OkResult(value="allowed-once")
        args: JsonObject = {}
        if frame.reason:
            args["reason"] = frame.reason
        requests = DeferredToolRequests(
            approvals=[
                ToolCallPart(
                    tool_name=frame.tool_name,
                    args=args,
                    tool_call_id=frame.call_id or frame.approval_id,
                    provider_name=DEEPSEEK_PROVIDER_NAME,
                )
            ]
        )
        batch, response = await self._await_human(context=context, requests=requests)
        action = next(iter(batch.approvals))
        approved = response is not None and bool(
            response.approvals.get(action.id, False)
        )
        if approved and response is not None and response.allow_session:
            context.session_allowed.add(frame.tool_name)
            await self.conversations.grant_session_tool(
                context.conversation,
                frame.tool_name,
            )
        if approved:
            return OkResult(value="allowed-once")
        if response is None:
            return ErrResult(
                error=RpcError(
                    code="cancelled",
                    message=f"The approval for {frame.tool_name} expired "
                    "without a response.",
                )
            )
        return OkResult(value="rejected")

    async def answer_questions(
        self, context: DeepseekBridgeContext, frame: QuestionRequestedFrame
    ) -> RpcResult:
        if not context.interactive:
            return ErrResult(
                error=RpcError(
                    code="cancelled",
                    message="This run is non-interactive; nobody can answer.",
                )
            )
        questions: list[QuestionRequest] = []
        for item in frame.questions:
            request = QuestionRequest(question=item.question)
            choices = [option.label for option in (item.options or [])][
                :MAX_QUESTION_CHOICES
            ]
            if choices:
                request["choices"] = choices
            if item.detail:
                request["hint"] = item.detail
            if item.multi_select:
                request["multi_select"] = True
            questions.append(request)
        requests = DeferredToolRequests(
            calls=[
                ToolCallPart(
                    tool_name="deepseek_user_input",
                    args={"questions": questions},
                    tool_call_id=str(uuid7()),
                    provider_name=DEEPSEEK_PROVIDER_NAME,
                )
            ]
        )
        batch, response = await self._await_human(context=context, requests=requests)
        if response is None:
            return ErrResult(
                error=RpcError(code="cancelled", message="The user did not answer.")
            )
        # Batch questions carry their position in the call's list, so sorting
        # them realigns each with the dsh item it was built from. An answer
        # matching an option label, and a multi-select question's picks, are echoed
        # pristine into `selected` — dsh matches answers by label — while anything
        # else is `custom` text, and no answer is an answered-but-empty item, which
        # dsh accepts as a skip.
        answers: list[JsonValue] = []
        for item, action in zip(frame.questions, sorted(batch.questions), strict=False):
            answer = response.answers.get(action.id)
            labels = {option.label for option in (item.options or [])}
            payload: JsonObject = {"id": item.id, "selected": []}
            if isinstance(answer, list):
                payload["selected"] = [*answer]
            elif answer and answer in labels:
                payload["selected"] = [answer]
            elif answer:
                payload["custom"] = answer
            answers.append(payload)
        return OkResult(value={"answers": answers})

    async def _await_human(
        self,
        *,
        context: DeepseekBridgeContext,
        requests: DeferredToolRequests,
    ) -> tuple[DeferredActionBatch, DeferredActionBatchResponse | None]:
        if context.suspender is None:
            raise RuntimeError("a dsh approval mid-turn needs a suspender to pause on")
        # Waiting before the cards go up, so a quick answer cannot miss it.
        batch_id: UUID7 = uuid7()
        future: asyncio.Future[DeferredActionBatchResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pendings[batch_id] = future
        try:
            batch, event = await context.suspender.pause(requests, batch_id=batch_id)
            if event is not None:
                context.frames.put_nowait(event)
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(future), self.config.approval_timeout
                )
            except TimeoutError:
                await self.deferred_actions.mark_batch(batch.id, "expired")
                return batch, None
        finally:
            self.pendings.pop(batch_id, None)
        await self.deferred_actions.resolve_batch(response)
        return batch, response

    @staticmethod
    def unwrap(result: RpcResult, method: str) -> JsonValue:
        """The business value, or the business failure as the run's failure —
        exactly as dsh reported it. Carrier failures arrive here too, already
        folded into the error branch by the client."""
        if isinstance(result, ErrResult):
            raise AgentRunError(
                f"dsh {method} failed: {result.error.message} ({result.error.code})"
            )
        return result.value

    async def sync_session_name(
        self, conversation: Conversation, session_id: str
    ) -> None:
        try:
            value = self.unwrap(
                await self.ink.client.remote(
                    "session/projections", {"request": {"sessionId": session_id}}
                ),
                "session/projections",
            )
            if value is None:
                return
            name = SessionProjectionsValue.model_validate(value).values.title
        except (AgentRunError, OSError, ValidationError):
            logger.warning(
                "dsh session name lookup failed for %s", session_id, exc_info=True
            )
            return
        if not name or not name.strip():
            return
        await self.conversations.set_name(conversation, name)
        if conversation.parent_conversation_id is not None:
            return
        thread = await self.threads.get(conversation.thread_id, with_messages=False)
        if thread is None:
            raise ValueError(f"unknown thread {conversation.thread_id}")
        await self.threads.rename(thread, name)

    async def _iter_events(
        self,
        user_prompt: str | Sequence[UserContent] | None,
        *,
        conversation_address: ChannelAddress,
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[None] = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
        deferred_suspender: DeferredSuspender | None = None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        deepseek_model = model.model_name if isinstance(model, Model) else model
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        client = self.ink.client
        if not self.ink.running:
            # The client exists from birth, but a run needs the mux pump: an
            # un-entered tentacle would prompt and then wait on frames forever.
            raise RuntimeError(
                "DeepseekTentacle.run requires the tentacle to be entered"
            )
        if output_type is not None:
            raise ValueError(
                "DeepseekTentacle does not support structured output: dsh's "
                "session/prompt has no output schema"
            )

        if conversation_id is not None:
            conversation = await self.conversations.get(
                conversation_id, with_history=False
            )
            if (
                conversation.agent_tentacle_id != self.id
                or conversation.thread_id != thread_id
            ):
                raise ValueError(
                    f"conversation {conversation_id} does not belong to "
                    f"({self.id!r}, {thread_id})"
                )
        else:
            conversation = await self.conversations.ensure(
                thread_id,
                agent_tentacle_id=self.id,
                with_history=False,
            )
        accumulator = DeepseekRunAccumulator()
        accumulator.begin(user_prompt)

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
            raise ValueError("DeepseekTentacle requires a non-empty text prompt")
        # dsh has no instructions channel on session/prompt, so run-level
        # instructions (a spawner's framing, mostly) travel inside the prompt —
        # marked, because everything else in there is what somebody said.
        if isinstance(instructions, str) and instructions:
            prompt_text = f"{tagged('instructions', instructions)}\n\n{prompt_text}"

        project = await self.run_project(conversation.thread_id)
        workspace = self.workspaces.open(conversation.thread_id, project)
        run_cwd = str(workspace.path)

        with deepseek_logfire.span(
            "DeepseekTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=run_name or "deepseek",
            conversation_address=str(conversation_address),
            **agent_input_message_attributes(user_prompt),
        ):
            # Entered first so it leaves last: the tree exists before dsh is given
            # it as a cwd, and a chat thread's is only thrown away once the turn
            # using it is finished with it.
            async with (
                self.conversation_locks.hold(str(conversation.id)),
                workspace,
            ):
                conversation = await self.conversations.get(
                    conversation.id, with_history=False
                )
                effort = await self.resolve_effort(
                    conversation, model=deepseek_model, effort=effort
                )
                permission_mode = (
                    conversation.permission_mode or self.config.permission_mode
                )
                self.check_permission_mode(permission_mode)
                session_id = conversation.external_id
                if not session_id:
                    create_payload: JsonObject = {"cwd": run_cwd}
                    if self.config.agent_preset is not None:
                        create_payload["agentPreset"] = self.config.agent_preset
                    created = SessionCreateValue.model_validate(
                        self.unwrap(
                            await client.remote(
                                "session/create", {"request": create_payload}
                            ),
                            "session/create",
                        )
                    )
                    session_id = created.session_id
                    await self.conversations.set_external_id(conversation, session_id)
                async with self.driving(session_id):
                    if deepseek_model is not None:
                        await self.ink.select_model(
                            session_id,
                            deepseek_model,
                            default_provider=self.default_provider,
                            reasoning_effort=effort,
                        )
                    await self.ink.set_permission_mode(session_id, permission_mode)

                    queue = await self.ink.subscribe(session_id)
                    self.bridge_contexts[session_id] = DeepseekBridgeContext(
                        conversation=conversation,
                        session_allowed=set(conversation.allowed_tools),
                        interactive=interactive,
                        suspender=deferred_suspender,
                        frames=queue,
                    )
                    prompted = False
                    try:
                        SessionPromptValue.model_validate(
                            self.unwrap(
                                await client.remote(
                                    "session/prompt",
                                    {
                                        "request": {
                                            "sessionId": session_id,
                                            "requestId": str(uuid4()),
                                            "mode": "queue",
                                            "content": [
                                                {"type": "text", "text": prompt_text}
                                            ],
                                        }
                                    },
                                ),
                                "session/prompt",
                            )
                        )
                        prompted = True
                        while not accumulator.turn_ended:
                            frame = await queue.get()
                            if isinstance(frame, ActionBatchEvent):
                                # A batch the turn paused on, for whoever draws the run.
                                yield frame
                                continue
                            if isinstance(frame, StreamErrorFrame):
                                accumulator.turn_error = f"dsh event stream failed mid-turn: {frame.error.message}"
                                break
                            if isinstance(frame, SessionAssistantFrame):
                                for event in accumulator.consume_assistant_stream(
                                    frame
                                ):
                                    yield event
                                continue
                            if (
                                self.config.instrument
                                and frame.event.type != "assistant/chunk"
                                and (
                                    accumulator.turn_started
                                    or frame.event.type == "turn/start"
                                )
                            ):
                                deepseek_logfire.info(
                                    "deepseek.event {event_type}",
                                    event_type=frame.event.type,
                                    session_id=session_id,
                                    event=frame.event.model_dump(mode="json"),
                                )
                            for event in accumulator.consume(frame):
                                yield event
                    finally:
                        self.bridge_contexts.pop(session_id, None)
                        if prompted and not accumulator.turn_ended:
                            with contextlib.suppress(Exception):
                                await client.remote(
                                    "session/cancel",
                                    {"request": {"sessionId": session_id}},
                                )
                        with contextlib.suppress(Exception):
                            await self.ink.unsubscribe(session_id)

                run_id = str(uuid7())
                recorded_run = await self.conversations.record_agent_run(
                    conversation,
                    run_id=run_id,
                    messages=accumulator.messages,
                    name=run_name,
                    model_name=accumulator.route.model
                    if accumulator.route is not None
                    else deepseek_model,
                    permission_mode=permission_mode,
                    cwd=Path(run_cwd),
                    external_id=session_id,
                    native_id=DEEPSEEK_NATIVE_ID,
                    native_turn_id=(
                        f"{session_id}:{accumulator.turn_number}"
                        if accumulator.turn_number is not None
                        else None
                    ),
                )
                await self.sync_session_name(conversation, session_id)
        if source_thread_message_ids:
            if recorded_run is None:
                raise RuntimeError("prompt-source bindings require a persisted dsh run")
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
            await self.threads.bind_messages(
                source_message_ids,
                prompt_request.id,
                kind="request_source",
                run_id=recorded_run.id,
            )
            source_thread = await self.threads.ensure(
                source_thread_address or conversation_address
            )
            await self.threads.advance_prompt_cursor(
                source_thread,
                source_message_ids[-1],
            )
        if accumulator.turn_error:
            raise AgentRunError(accumulator.turn_error)
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
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
        result: AgentRunResult[str] | None = None
        async for event in self.observe_run(
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
                conversation_id=conversation_id,
                interactive=interactive,
                instructions=instructions,
                capabilities=capabilities,
                deferred_suspender=deferred_suspender,
            )
        ):
            if isinstance(event, AgentRunResultEvent):
                result = event.result
        if result is None:
            raise RuntimeError("dsh run completed without a result")
        return result

    @overload
    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_address: ChannelAddress,
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT],
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
        thread_id: UUID7 | None = None,
        source_thread_address: ChannelAddress | None = None,
        source_thread_message_ids: Sequence[UUID7] | None = None,
        run_name: str | None = None,
        output_type: OutputSpec[RunOutputDataT] | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        model: Model | KnownModelName | str | None = None,
        effort: str | None = None,
        conversation_id: UUID7 | None = None,
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
        return ReactEventStream(
            self.observe_run(
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
                    conversation_id=conversation_id,
                    interactive=interactive,
                    instructions=instructions,
                    capabilities=capabilities,
                    deferred_suspender=deferred_suspender,
                )
            )
        )
