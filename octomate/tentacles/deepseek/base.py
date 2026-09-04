from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, get_args, overload
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from octomate_cli.stream import (
    SESSION_FILE,
    STREAM_PROTOCOL,
    StreamEof,
    StreamFinalize,
    StreamHello,
    StreamWelcome,
    client_message_adapter,
)
from pydantic import HttpUrl, ValidationError
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
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from rich.style import Style
from uuid_utils.compat import uuid7
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import DeepseekConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    DeferredActionBatch,
    QuestionRequest,
)
from octomate.schemas.messages import ModelRequest
from octomate.schemas.thread import DEEPSEEK_NATIVE_ID
from octomate.schemas.user import UserProfile
from octomate.telemetry import deepseek_logfire
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle
from octomate.tentacles.deepseek.adapter import (
    DEEPSEEK_PROVIDER_NAME,
    DeepseekRunAccumulator,
)
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.hooks import DeepseekHookInput
from octomate.tentacles.deepseek.ingest import DeepseekHookIngest
from octomate.tentacles.deepseek.process import DeepseekProcess
from octomate.tentacles.deepseek.tailer import DeepseekEventTailer
from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    CommandExecutionValue,
    ErrResult,
    OkResult,
    QuestionRequestedFrame,
    RpcError,
    RpcResult,
    SessionCreateValue,
    SessionEventFrame,
    SessionPromptValue,
    StreamErrorFrame,
)
from octomate.tentacles.hooks import hook_guard, hook_sender
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject, JsonValue
from octomate.types.permissions import DeepseekPermissionMode, is_deepseek_mode

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


@dataclass
class DeepseekBridgeContext:
    conversation: Conversation
    conversation_address: ChannelAddress
    run_name: str | None
    session_allowed: set[str]
    interactive: bool


@dataclass
class DeepseekTentacle(AgentTentacle[str, None]):
    """DeepSeek Harness (dsh) exposed as an Octomate agent tentacle.

    Attach first, start second: a dsh already serving the configured
    `host:port` is used as it stands — the one the operator runs — and a
    `dsh web` child is started there only when nothing answers, because two
    harnesses over one `$DSH_HOME` interleave unlocked session-log appends and
    corrupt them. Only a child of our own is stopped on exit. Either way the
    tentacle drives the harness over the `/api` gateway — HTTP POSTs for unary
    calls, the all-session mux WebSocket for events, `POST /api/respond` for
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
    tailer to assemble into turns. Sessions this tentacle drives itself are
    claimed (`DeepseekHookIngest.driving`) so their hooks are dropped and
    their tails refused rather than recorded twice.
    """

    config: DeepseekConfig = field(init=False)
    process: DeepseekProcess | None = field(default=None, init=False, repr=False)
    client: DeepseekApiClient = field(init=False, repr=False)
    mux_socket: ClientConnection | None = field(default=None, init=False, repr=False)
    mux_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    closing: bool = field(default=False, init=False)
    subscribers: dict[str, asyncio.Queue[SessionEventFrame | StreamErrorFrame]] = field(
        default_factory=dict, init=False
    )
    bridge_contexts: dict[str, DeepseekBridgeContext] = field(
        default_factory=dict, init=False
    )
    interaction_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    # dsh approvals/questions are answered in-process through mux frames while
    # the turn stays live. `pending` parks the card response futures.
    in_process: ClassVar[bool] = True

    permission_modes: ClassVar[tuple[str, ...]] = get_args(DeepseekPermissionMode)

    @property
    def default_permission_mode(self) -> str | None:
        return self.config.permission_mode

    # DeepSeek's own blue, so dsh's lines read as dsh's in a shared console.
    brand_color: ClassVar[Style | None] = Style(color="#4D6BFE", bold=True)

    description: str = (
        "DeepSeek Harness coding agent for repository-aware software engineering tasks."
    )

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: DeepseekConfig,
        description: str | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.description = description or self.description
        self.process = None
        # The endpoint is fixed by config, so the client lives as long as the
        # tentacle — attach, launch, runs and teardown all speak through it.
        endpoint = HttpUrl(f"http://{config.host}:{config.port}")
        self.client = DeepseekApiClient(
            base_url=endpoint, http_client=httpx.AsyncClient(base_url=str(endpoint))
        )
        self.mux_socket = None
        self.mux_task = None
        self.closing = False
        self.subscribers = {}
        self.bridge_contexts = {}
        self.interaction_tasks = set()
        self.pending = {}
        # Not `dict(...)`, which C416 asks for: each config's claims are keyed by that
        # runtime's own narrower literal, and `Mapping`'s key is invariant — only the
        # comprehension widens them to `AgentRouteModelName` without a cast.
        self.claims = {  # noqa: C416
            model: claim for model, claim in config.claims.items()
        }
        self.gateway = config.gateway
        self.models = {model: model for model in config.models}
        # Serializes turns per conversation: dsh queues a second prompt into a
        # live turn as steering, which would interleave two runs' frames.
        self.conversation_locks = SessionLocks()
        self.session_locks = SessionLocks()
        self.session_tailer = DeepseekEventTailer(
            self.octomate.conversations,
            self.octomate.thread_manager,
            self.octomate.projects,
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
        verifier = hook_guard(self.octomate.bearers, self.id)
        resolve_sender = hook_sender(self.octomate.users, DEEPSEEK_NATIVE_ID, verifier)
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
        """One remote tail's connection, up to its attach: take the hello and
        refuse what cannot stream — a stale protocol loudly, and a session this
        tentacle is driving itself, whose events ingested here would write the
        conversation a second time (`DeepseekHookIngest.driving`). `sender` is
        the verified bearer's own profile, resolved at the handshake — whose
        ledger this stream writes."""
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
        if hello.session_id in self.session_ingest.driven:
            await websocket.close(code=1008, reason="octomate drives this session")
            return
        # Its own materia context: a stream outlives any request.
        with sqlalchemy_materia():
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

    async def attach_or_start(self) -> DeepseekProcess | None:
        """The harness behind the client's endpoint, attach-first: a dsh
        already serving there is used as it stands, and one is started only
        when nothing answers — bound to that same fixed port, so the next
        octomate finds it rather than starting a second writer of one
        `$DSH_HOME`. The config keeps `host` loopback, so a started child
        answers the endpoint the client already points at; either way the
        client is verified before this returns."""
        if await self.client.answering():
            logger.info("attached to the dsh serving %s", self.client.base_url)
            return None
        process = DeepseekProcess(
            executable=self.config.executable,
            port=self.config.port,
            extra_args=list(self.config.extra_args),
            dsh_home=self.config.dsh_home,
            ready_timeout=self.config.ready_timeout,
        )
        try:
            base_url = await process.start()
        except Exception:
            # Two starters waking together both find the port silent; one binds
            # it and the other dies on the address. Losing that race means a
            # dsh is serving after all — attach to the winner rather than
            # reporting a failure that has already fixed itself.
            if await self.client.answering():
                logger.info(
                    "lost the start race; attached to the dsh serving %s",
                    self.client.base_url,
                )
                return None
            raise
        if not await self.client.answering():
            await process.stop()
            raise RuntimeError(
                f"dsh reported {base_url} but does not answer at {self.client.base_url}"
            )
        logger.warning(
            "started dsh at %s — dsh has no cross-process lock on session logs, "
            "so another `dsh web` against the same DSH_HOME can corrupt them",
            base_url,
        )
        return process

    async def __aenter__(self) -> DeepseekTentacle:
        self.closing = False
        await self.client.__aenter__()
        # attach_or_start leaves the client verified (host.describe answered);
        # the mux socket must then be open before anything prompts, so a run's
        # first frames cannot outrun the subscribed baseline. A failed
        # handshake is a broken harness, not weather: fail the start rather
        # than retrying.
        try:
            self.process = await self.attach_or_start()
            socket = await self.client.open_mux()
        except BaseException:
            await self.client.__aexit__()
            if self.process is not None:
                await self.process.stop()
            self.process = None
            raise
        self.mux_socket = socket
        self.mux_task = asyncio.create_task(self.pump_mux(self.client, socket))
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closing = True
        self.session_ingest.shutdown()
        await self.session_tailer.shutdown()
        for session_id in list(self.subscribers):
            with contextlib.suppress(Exception):
                await self.client.call("session.cancel", {"sessionId": session_id})
        if self.mux_task is not None:
            self.mux_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.mux_task
            self.mux_task = None
        if self.mux_socket is not None:
            with contextlib.suppress(Exception):
                await self.mux_socket.close()
            self.mux_socket = None
        for task in list(self.interaction_tasks):
            task.cancel()
        self.interaction_tasks.clear()
        for future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        self.pending.clear()
        self.bridge_contexts.clear()
        await self.client.__aexit__(*exc)
        if self.process is not None:
            await self.process.stop()
            self.process = None

    async def pump_mux(
        self, client: DeepseekApiClient, socket: ClientConnection
    ) -> None:
        """Route the one mux stream: session events to their run's queue,
        answerable frames to the interaction bridge, everything else dropped.
        The stream ending while runs are live is a failure those runs must see
        — there is no reconnect loop, because the child is ours on loopback and
        a dropped socket there is not weather."""
        try:
            async for rpc_id, frame in client.mux_frames(socket):
                if isinstance(frame, SessionEventFrame):
                    queue = self.subscribers.get(frame.session_id)
                    if queue is not None:
                        queue.put_nowait(frame)
                elif isinstance(frame, ApprovalRequestedFrame | QuestionRequestedFrame):
                    task = asyncio.create_task(self.answer_interaction(rpc_id, frame))
                    self.interaction_tasks.add(task)
                    task.add_done_callback(self.interaction_tasks.discard)
                elif isinstance(frame, StreamErrorFrame):
                    for queue in self.subscribers.values():
                        queue.put_nowait(frame)
        except ConnectionClosed:
            pass
        finally:
            if not self.closing:
                failure = StreamErrorFrame(
                    type="stream/error",
                    error=RpcError(code="internal", message="dsh event stream closed"),
                )
                for queue in self.subscribers.values():
                    queue.put_nowait(failure)

    async def answer_interaction(
        self, rpc_id: str, frame: ApprovalRequestedFrame | QuestionRequestedFrame
    ) -> None:
        client = self.client
        context = self.bridge_contexts.get(frame.session_id)
        if context is None:
            # A request nobody is driving must not block dsh forever.
            result: RpcResult = ErrResult(
                error=RpcError(
                    code="cancelled",
                    message="Octomate has no live run for this session.",
                )
            )
        else:
            try:
                if isinstance(frame, ApprovalRequestedFrame):
                    result = await self.answer_approval(context, frame)
                else:
                    result = await self.answer_questions(context, frame)
            except Exception as error:
                logger.exception(
                    "session %s: answering a dsh %s failed",
                    frame.session_id,
                    frame.type,
                )
                result = ErrResult(error=RpcError(code="cancelled", message=str(error)))
        receipt = await client.respond(rpc_id, result)
        if not receipt.accepted:
            # Normal race: another settlement (a cancel, usually) got there first.
            logger.debug(
                "session %s: dsh dropped the %s response (%s)",
                frame.session_id,
                frame.type,
                receipt.reason,
            )

    async def answer_approval(
        self, context: DeepseekBridgeContext, frame: ApprovalRequestedFrame
    ) -> RpcResult:
        def outcome(decision: str) -> RpcResult:
            return OkResult(
                value={
                    "sessionId": frame.session_id,
                    "approvalId": frame.approval_id,
                    "outcome": decision,
                }
            )

        if not context.interactive:
            # A commissioned run has no human, and dsh's ask-vs-never lives
            # inside the preset rather than in a swappable posture, so the
            # non-interactive contract is declining at the bridge.
            return outcome("rejected")
        if frame.tool_name in context.session_allowed:
            return outcome("allowed-once")
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
            await self.octomate.conversations.grant_session_tool(
                context.conversation,
                frame.tool_name,
            )
        if approved:
            return outcome("allowed-once")
        if response is None:
            return ErrResult(
                error=RpcError(
                    code="cancelled",
                    message=f"The approval for {frame.tool_name} expired "
                    "without a response.",
                )
            )
        return outcome("rejected")

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
            questions.append(request)
        requests = DeferredToolRequests(
            calls=[
                ToolCallPart(
                    tool_name="deepseek_user_input",
                    args={"questions": questions},
                    tool_call_id=str(uuid4()),
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
        # matching an option label is echoed pristine into `selected` — dsh
        # matches answers by label — anything else is `custom` text, and no
        # answer is an answered-but-empty item, which dsh accepts as a skip.
        answers: list[JsonValue] = []
        for item, action in zip(frame.questions, sorted(batch.questions), strict=False):
            answer = response.answers.get(action.id)
            labels = {option.label for option in (item.options or [])}
            payload: JsonObject = {"id": item.id, "selected": []}
            if answer and answer in labels:
                payload["selected"] = [answer]
            elif answer:
                payload["custom"] = answer
            answers.append(payload)
        value: JsonObject = {
            "sessionId": frame.session_id,
            "answer": {"answers": answers},
        }
        return OkResult(value=value)

    async def _await_human(
        self,
        *,
        context: DeepseekBridgeContext,
        requests: DeferredToolRequests,
    ) -> tuple[DeferredActionBatch, DeferredActionBatchResponse | None]:
        channel = self.octomate.channels.get(
            context.conversation_address.channel_tentacle_id
        )
        if channel is None:
            raise RuntimeError(
                "no channel "
                f"{context.conversation_address.channel_tentacle_id!r} to present "
                "a dsh approval/question"
            )
        batch = await channel.feelers.present_actions(
            action_manager=self.octomate.deferred_actions,
            conversation=context.conversation,
            agent_tentacle_id=self.id,
            run_name=context.run_name,
            source_address=context.conversation_address,
            target_address=context.conversation_address,
            target_mode="sub"
            if context.conversation_address.channel_thread_id
            else "main",
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
        except TimeoutError:
            await self.octomate.deferred_actions.mark_batch(batch.id, "expired")
            return batch, None
        finally:
            self.pending.pop(batch.id, None)
        await self.octomate.deferred_actions.resolve_batch(response)
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
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
        instructions: AgentInstructions[None] = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        client = self.client
        if self.mux_task is None:
            # The client exists from birth, but a run needs the mux pump: an
            # un-entered tentacle would prompt and then wait on frames forever.
            raise RuntimeError(
                "DeepseekTentacle.run requires the tentacle to be entered"
            )
        if output_type is not None:
            raise ValueError(
                "DeepseekTentacle does not support structured output: dsh's "
                "session.prompt has no output schema"
            )

        if conversation_id is not None:
            conversation = await self.octomate.conversations.get(conversation_id)
            if (
                conversation.agent_tentacle_id != self.id
                or conversation.thread_id != thread_id
            ):
                raise ValueError(
                    f"conversation {conversation_id} does not belong to "
                    f"({self.id!r}, {thread_id})"
                )
        else:
            conversation = await self.octomate.conversations.ensure(
                thread_id,
                agent_tentacle_id=self.id,
            )
        accumulator = DeepseekRunAccumulator()
        accumulator.begin(user_prompt)

        if isinstance(model, Model):
            deepseek_model = model.model_name
        elif isinstance(model, str):
            deepseek_model = model
        else:
            deepseek_model = None

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
        # dsh has no instructions channel on session.prompt, so run-level
        # instructions (a spawner's framing, mostly) travel as prompt framing.
        if isinstance(instructions, str) and instructions:
            prompt_text = f"{instructions}\n\n{prompt_text}"

        permission_mode = (
            conversation.permission_mode
            if is_deepseek_mode(conversation.permission_mode)
            else self.config.permission_mode
        )
        project = await self.run_project(conversation.thread_id)
        workspace = self.octomate.workspaces.open(conversation.thread_id, project)
        run_cwd = str(workspace.path)

        with deepseek_logfire.span(
            "DeepseekTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=run_name or "deepseek",
            conversation_address=str(conversation_address),
        ):
            # Entered first so it leaves last: the tree exists before dsh is given
            # it as a cwd, and a chat thread's is only thrown away once the turn
            # using it is finished with it.
            async with (
                workspace,
                self.conversation_locks.hold(str(conversation.id)),
            ):
                session_id = conversation.external_id
                if not session_id:
                    create_payload: JsonObject = {"cwd": run_cwd}
                    if self.config.agent_preset is not None:
                        create_payload["agentPreset"] = self.config.agent_preset
                    created = SessionCreateValue.model_validate(
                        self.unwrap(
                            await client.call("session.create", create_payload),
                            "session.create",
                        )
                    )
                    session_id = created.session_id
                if deepseek_model is not None:
                    select_payload: JsonObject = {
                        "sessionId": session_id,
                        "provider": self.config.provider,
                        "model": deepseek_model,
                    }
                    reasoning_effort = (
                        self.config.efforts.get(effort) if effort is not None else None
                    )
                    if reasoning_effort is not None:
                        select_payload["reasoningEffort"] = reasoning_effort
                    self.unwrap(
                        await client.call("session.selectModel", select_payload),
                        "session.selectModel",
                    )
                # No permission RPC exists: the preset switches through the
                # remotes-plane command, which opens no turn.
                executed = self.unwrap(
                    await client.remote(
                        "commands/execute",
                        {
                            "agentId": session_id,
                            "line": f"/permission {permission_mode}",
                        },
                    ),
                    "commands/execute",
                )
                if executed is None:
                    raise AgentRunError(
                        "dsh has no /permission command, so the run's posture "
                        f"({permission_mode}) cannot be set"
                    )
                execution = CommandExecutionValue.model_validate(executed)
                if execution.result is not None and execution.result.kind == "error":
                    raise AgentRunError(
                        f"dsh refused /permission {permission_mode}: "
                        f"{execution.result.text or 'unknown preset'}"
                    )

                # Subscribe before prompting, so the turn's first frames cannot
                # slip between the prompt and the queue.
                queue: asyncio.Queue[SessionEventFrame | StreamErrorFrame] = (
                    asyncio.Queue()
                )
                self.subscribers[session_id] = queue
                self.bridge_contexts[session_id] = DeepseekBridgeContext(
                    conversation=conversation,
                    conversation_address=conversation_address,
                    run_name=run_name,
                    session_allowed=set(conversation.allowed_tools),
                    interactive=interactive,
                )
                prompted = False
                # Claimed before the prompt goes out: the turn's hooks —
                # `UserPromptSubmit` at pre-step, `Stop` inside turn-stopping,
                # both ahead of the `turn/end` that ends this scope — must
                # arrive claimed, or the native ingest would write this driven
                # conversation a second time.
                with self.session_ingest.driving(session_id):
                    try:
                        prompt_value = SessionPromptValue.model_validate(
                            self.unwrap(
                                await client.call(
                                    "session.prompt",
                                    {
                                        "sessionId": session_id,
                                        "mode": "queue",
                                        "content": [
                                            {"type": "text", "text": prompt_text}
                                        ],
                                    },
                                ),
                                "session.prompt",
                            )
                        )
                        if prompt_value.command is not None:
                            # dsh intercepted the line as a slash command: no
                            # turn opened, the command's answer is the whole
                            # result.
                            command_text = (
                                prompt_value.command.text
                                or f"{prompt_value.command.kind} command executed"
                            )
                            for event in accumulator.complete_command(command_text):
                                yield event
                        else:
                            prompted = True
                            while not accumulator.turn_ended:
                                frame = await queue.get()
                                if isinstance(frame, StreamErrorFrame):
                                    accumulator.turn_error = (
                                        "dsh event stream failed mid-turn: "
                                        f"{frame.error.message}"
                                    )
                                    break
                                for event in accumulator.consume(frame):
                                    yield event
                    finally:
                        self.subscribers.pop(session_id, None)
                        self.bridge_contexts.pop(session_id, None)
                        if prompted and not accumulator.turn_ended:
                            # The run is leaving mid-turn (cancelled, or its
                            # stream died); don't leave dsh's turn burning.
                            with contextlib.suppress(Exception):
                                await client.call(
                                    "session.cancel", {"sessionId": session_id}
                                )

                run_id = str(uuid7())
                recorded_run = await self.octomate.conversations.record_agent_run(
                    conversation,
                    run_id=run_id,
                    messages=accumulator.messages,
                    name=run_name,
                    cwd=Path(run_cwd),
                    external_id=session_id,
                )
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
            conversation_id=conversation_id,
            interactive=interactive,
            instructions=instructions,
            capabilities=capabilities,
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
                conversation_id=conversation_id,
                interactive=interactive,
                instructions=instructions,
                capabilities=capabilities,
            )
        )
