from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast, get_args, overload

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
from pydantic import SecretStr, TypeAdapter, ValidationError
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

from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import CodexConfig
from octomate.mcp.gateway import CONVERSATION_HEADER, GATEWAY_SERVER_NAME
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
)
from octomate.schemas.deferred import DeferredActionBatch, QuestionRequest
from octomate.schemas.messages import ModelRequest
from octomate.schemas.thread import CODEX_NATIVE_ID, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.telemetry import codex_logfire
from octomate.tentacles.agents.base import AgentSpecInput, AgentTentacle
from octomate.tentacles.agents.codex.adapter import (
    CODEX_PROVIDER_NAME,
    CodexRunAccumulator,
    json_object_adapter,
)
from octomate.tentacles.agents.codex.hooks import (
    DRIVEN_ENV,
    CodexHookInput,
)
from octomate.tentacles.agents.codex.ingest import CodexHookIngest
from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.agents.hooks import hook_guard, hook_sender
from octomate.tentacles.agents.locks import SessionLocks
from octomate.types.json import JsonObject
from octomate.types.permissions import CodexPermissionMode, is_codex_mode

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


# The public openai_codex API can't express two things the `user` approval bridge
# needs: an approval-handler callback on the async client, and the
# `approvals_reviewer=user` thread option. Both are reached through underscored SDK
# internals (`AsyncCodexClient._sync` in `CodexTentacle.__aenter__`, and
# `_client.thread_start` / `_sandbox_mode` in the thread start/resume helpers),
# pinned to openai-codex 0.1.0b3; a Codex upgrade that changes them surfaces there.


@dataclass(frozen=True)
class CodexPermissionPlan:
    """One Codex posture, unpacked into the SDK's approval knobs.

    Approvals only. The sandbox is the other axis — what a command may touch when
    nobody is asked — and it is the run's, from `CodexConfig.sandbox`, so a posture
    cannot quietly widen or narrow what the thread reaches.
    """

    # Public thread/turn `approval_mode`, or None when only the reviewer path fits.
    sdk_mode: ApprovalMode | None
    # Equivalent raw params for the private `user`-reviewer path.
    policy: AskForApproval
    reviewer: ApprovalsReviewer | None


# The one mapping from a Codex posture onto the SDK's own enums: who answers when the
# agent asks to step past the sandbox.
#
# Only `user_review` needs the private reviewer path — the SDK has no public knob for
# `approvals_reviewer=user` — so the pin to openai-codex 0.1.0b3 narrows to that row.
CODEX_PERMISSION_PLANS: dict[CodexPermissionMode, CodexPermissionPlan] = {
    "user_review": CodexPermissionPlan(
        sdk_mode=None,
        policy=AskForApproval(root=AskForApprovalValue.on_request),
        reviewer=ApprovalsReviewer.user,
    ),
    "auto_review": CodexPermissionPlan(
        sdk_mode=ApprovalMode.auto_review,
        policy=AskForApproval(root=AskForApprovalValue.on_request),
        reviewer=ApprovalsReviewer.auto_review,
    ),
    "deny_all": CodexPermissionPlan(
        sdk_mode=ApprovalMode.deny_all,
        policy=AskForApproval(root=AskForApprovalValue.never),
        reviewer=None,
    ),
}


# `workspace_write` ships `networkAccess: false`, so every driven Codex turn has run
# without a network. Octomate is a relay: a run that cannot reach the registry, the
# API or the remote it is working against is broken rather than safer, and the other
# runtimes have never been closed this way. The app-server resolves the preset against
# its own config, so this opens the network without widening what a command may write.
NETWORK_ACCESS = "sandbox_workspace_write.network_access=true"

# How a driven turn's Codex process is told to reach the gateway MCP server: the
# launch config names these variables, and `new_client` fills them per conversation,
# so the identity the header asserts is the launch config's and never the model's.
# The token is the kicking user's own secret — the turn speaks as the human it
# represents, and a kicker carrying none gets no wiring at all.
GATEWAY_TOKEN_ENV = "OCTOMATE_GATEWAY_TOKEN"
GATEWAY_CONVERSATION_ENV = "OCTOMATE_GATEWAY_CONVERSATION"


@dataclass
class PooledCodexClient:
    client: AsyncCodex
    # The live SDK thread handle for this Octomate thread; created on first turn and
    # reused so later turns continue the same Codex thread on the warm process.
    thread: AsyncThread | None = None
    last_used: float = 0.0
    # Active runs holding this client; a client with `in_use > 0` is never evicted.
    in_use: int = 0
    # The kicker's bearer this client's gateway wiring was launched with — None for
    # no wiring. A turn wanting otherwise evicts and rebuilds, since launch config
    # is fixed at process start.
    gateway_bearer: SecretStr | None = None


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
        build: Callable[[uuid.UUID, SecretStr | None], AsyncCodex],
        max_clients: int | None,
        idle_ttl: float | None,
    ) -> None:
        self.build = build
        self.max_clients = max_clients
        self.idle_ttl = idle_ttl
        self.clients: OrderedDict[uuid.UUID, PooledCodexClient] = OrderedDict()
        self.lock = asyncio.Lock()

    async def acquire(
        self, conversation_id: uuid.UUID, *, gateway_bearer: SecretStr | None = None
    ) -> PooledCodexClient:
        async with self.lock:
            await self.evict_idle()
            pooled = self.clients.get(conversation_id)
            if pooled is not None and pooled.gateway_bearer != gateway_bearer:
                # Launch config is fixed at process start, so a turn whose gateway
                # wiring disagrees gets a fresh process; the Codex thread itself
                # survives, resumed from the conversation's external id.
                if pooled.in_use:
                    raise RuntimeError(
                        f"conversation {conversation_id} has a live turn on a Codex "
                        "client whose gateway wiring disagrees with this turn's"
                    )
                del self.clients[conversation_id]
                await self.close(pooled)
                pooled = None
            if pooled is None:
                client = self.build(conversation_id, gateway_bearer)
                await client.__aenter__()
                pooled = PooledCodexClient(client=client, gateway_bearer=gateway_bearer)
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

    permission_modes: ClassVar[tuple[str, ...]] = get_args(CodexPermissionMode)

    @property
    def default_permission_mode(self) -> str | None:
        return self.config.permission_mode

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
        description: str | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.description = description or self.description
        self.pool = None
        self.live_turns = {}
        self.bridge_contexts = {}
        self.pending = {}
        # Not `dict(...)`, which C416 asks for: each config's claims are keyed by that
        # runtime's own narrower literal, and `Mapping`'s key is invariant — only the
        # comprehension widens them to `AgentRouteModelName` without a cast.
        self.claims = {  # noqa: C416
            model: claim for model, claim in config.claims.items()
        }
        self.gateway = config.gateway
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
        )

    def routers(self) -> tuple[APIRouter]:
        return (self.hook_router,)

    @cached_property
    def hook_router(self) -> APIRouter:
        """The hook pipe native Codex sessions POST their events into, and the stream
        endpoint every session's client-side tail feeds raw rollout lines through
        (`octomate codex tail`) — the server never reads a rollout from disk, this
        machine's sessions included. The guard covers the websocket too: FastAPI
        runs router dependencies at the handshake, so a bad bearer is denied with
        the same 401 before any socket opens. Each route takes `hook_sender` — the
        verified bearer resolved to their own profile, on the guard's single
        per-request check — as the ledger's principal."""
        verifier = hook_guard(self.octomate.bearers, self.id)
        resolve_sender = hook_sender(self.octomate.users, CODEX_NATIVE_ID, verifier)
        router = APIRouter(tags=["codex"], dependencies=[Depends(verifier)])

        @router.post("/hooks/codex", summary="Codex native-session hook pipe")
        async def receive_hook(
            event: CodexHookInput,
            # `param: T = Depends(dep)` is FastAPI's own dependency contract;
            # ruff's B008 exemption misses it when T is a custom class (it is
            # fine with `str`), so the rule bends rather than the checked type.
            sender: UserProfile = Depends(resolve_sender),  # noqa: B008
        ) -> JSONResponse:
            await self.session_ingest.handle(event, sender)
            return JSONResponse({})

        @router.websocket("/hooks/codex/stream")
        async def stream(
            websocket: WebSocket,
            sender: UserProfile = Depends(resolve_sender),  # noqa: B008
        ) -> None:
            await self.stream_session(websocket, sender)

        return router

    async def stream_session(self, websocket: WebSocket, sender: UserProfile) -> None:
        """One remote tail's connection, up to its attach: take the hello and refuse
        what cannot stream — a stale protocol loudly (the session still degrades to
        hooks-only ingest), and a session this tentacle is driving itself, whose
        rollout ingested here would write the conversation a second time
        (`CodexHookIngest.driving`). Authentication already happened at the
        handshake, and `sender` is the verified bearer's own profile — whose
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
        # Its own materia context: a stream outlives any request, like a follow loop.
        with sqlalchemy_materia():
            await self.stream_attached(websocket, hello, sender)

    async def stream_attached(
        self, websocket: WebSocket, hello: StreamHello, sender: UserProfile
    ) -> None:
        """The attached half of a stream connection: register the session, answer
        resume offsets, then feed each framed line through the tailer's assembly.
        Codex turns close on their own `task_complete`/`turn_aborted` lines, so
        nothing commits at the boundary either way — `eof` and a drop alike just
        return the registry slot, and the next connect re-streams from byte 0. A
        `Stop` on the hook pipe reaches here as the state's `stop_event`
        (`stop_turn`, once the stopped turn is durable or its wait ran out); the
        relay sends `finalize`, whose drain re-reads the client's files to EOF —
        rescuing bytes a missed watch wake left behind — and the tail exits until
        the next prompt's launcher. Codex fires no session-end hook, so a session
        that just stops ends by the client's own idle drain."""
        # The thread before the attach, filed under the project its cwd names —
        # `CodexHookIngest.session_thread`'s ordering, because `attach_remote` falls
        # back to a project-less create and a thread's project is frozen at creation.
        # Only a session on this same machine gets one: a project names server-local
        # directories, and a remote cwd naming one of them would be a false match.
        # TODO: assign remote sessions their project once projects can span machines.
        client = websocket.client
        local_client = client is not None and client.host in {"127.0.0.1", "::1"}
        project = None
        if local_client and hello.cwd:
            holder = self.octomate.projects.resolve(Path(hello.cwd))
            project = self.octomate.projects.get(holder) if holder is not None else None
        await self.octomate.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "thread", hello.session_id),
            project=project,
        )
        state, offsets = await self.session_tailer.attach_remote(
            hello.session_id,
            Path(hello.transcript_path),
            sender,
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
                # The socket died first; the drain this asked for cannot happen, and
                # the next connect re-streams what it would have shipped.
                logger.debug(
                    "session %s: finalize relay lost its socket", hello.session_id
                )

        relay = asyncio.create_task(relay_finalize())
        # Per-file contiguity: each line must start where the last one ended, so a
        # dropped frame surfaces as a close (4000 — the client reconnects and re-asks)
        # instead of a silently mis-assembled turn.
        expected_offsets = dict(offsets)
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
                key = message.agent_id or SESSION_FILE
                want = expected_offsets.get(key, 0)
                if message.start != want:
                    await websocket.close(
                        code=4000,
                        reason=f"offset gap for {key or 'session'}: expected {want}, "
                        f"got {message.start}",
                    )
                    return
                expected_offsets[key] = message.end
                await self.session_tailer.feed_remote(
                    state, message.agent_id, message.line, message.start, message.end
                )
        except WebSocketDisconnect:
            pass
        except ValidationError:
            await websocket.close(code=1008, reason="unparseable stream message")
        except Exception:
            logger.exception(
                "session %s: remote tail errored; its open turns are left for the "
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

    async def __aenter__(self) -> CodexTentacle:
        # Only the pool is tentacle-wide shared state; each conversation's Codex
        # client is built here, entered/exited through the SDK's async context, and
        # reused or evicted by the pool.
        def new_client(
            conversation_id: uuid.UUID, gateway_bearer: SecretStr | None
        ) -> AsyncCodex:
            env = {**(self.config.runtime.env or {}), DRIVEN_ENV: "1"}
            # First, so an operator who sets the key themselves still wins: later
            # `--config` arguments are the ones Codex keeps.
            overrides = (NETWORK_ACCESS, *self.config.runtime.config_overrides)
            # Either way the launch config says what `mcp_servers.gateway` is for
            # this process, because `~/.codex/config.toml` may already hold one:
            # the operator's own native entry, carrying the credential of whoever
            # ran `octomate codex mcp install` there. An app-server is a child of
            # this host and reads that file, so a turn that named nothing would
            # inherit a stranger's identity — every spell it cast would land on
            # their linked accounts. A turn speaks as its kicker or it does not
            # speak at all.
            if gateway_bearer is None:
                overrides = (
                    *overrides,
                    f"mcp_servers.{GATEWAY_SERVER_NAME}.enabled=false",
                )
            else:
                # The turn's session is at the gateway, so the launch config wires
                # the process to the served MCP endpoint and asserts the turn's own
                # conversation id — the model never chooses the header. The url is
                # the deployment config's port and path over loopback: the bind is
                # uvicorn's rather than the app's, and the app-server is a child
                # of the host being served.
                deployment = self.octomate.config
                if deployment is None:
                    raise RuntimeError(
                        "this turn's gateway session is registered, but the host "
                        "cannot name the served MCP endpoint to wire it to: "
                        "Octomate.config must be set"
                    )
                env[GATEWAY_TOKEN_ENV] = gateway_bearer.get_secret_value()
                env[GATEWAY_CONVERSATION_ENV] = str(conversation_id)
                url = (
                    f"http://127.0.0.1:{deployment.port}"
                    f"/{GATEWAY_SERVER_NAME}{deployment.mcp_path}"
                )
                overrides = (
                    *overrides,
                    f"mcp_servers.{GATEWAY_SERVER_NAME}.enabled=true",
                    f"mcp_servers.{GATEWAY_SERVER_NAME}.url={url}",
                    f"mcp_servers.{GATEWAY_SERVER_NAME}.bearer_token_env_var="
                    f"{GATEWAY_TOKEN_ENV}",
                    # Emptied rather than left alone: the native entry's own
                    # `Authorization` lives here, and it would outrank the bearer
                    # this turn just named.
                    f"mcp_servers.{GATEWAY_SERVER_NAME}.http_headers={{}}",
                    f"mcp_servers.{GATEWAY_SERVER_NAME}.env_http_headers="
                    f'{{"{CONVERSATION_HEADER}" = "{GATEWAY_CONVERSATION_ENV}"}}',
                )
            runtime = replace(self.config.runtime, env=env, config_overrides=overrides)
            client = AsyncCodex(config=runtime)

            # One client per conversation, so bind the approval handler to this
            # conversation id — no guessing which live context an SDK request belongs
            # to. The public constructor has no handler hook, so swap in a sync client
            # that has one (SDK-private) before the transport lazily starts.
            #
            # Always bound, because the posture is now the conversation's rather than
            # the tentacle's: the client is built before the run resolves one, and a
            # handler no request reaches is inert — only `user_review` sends any.
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
        # Only "allow for session" grants are left to check here. A posture that
        # forgoes review says so to the SDK — `auto_review` reviews there, and the
        # sandbox bounds the rest — so no request reaches this bridge to short-circuit.
        return tool_name in context.session_allowed

    @staticmethod
    def is_question_request(method: str) -> bool:
        lowered = method.lower()
        return "elicitation" in lowered or "question" in lowered

    @staticmethod
    def question_from_sdk_request(method: str, params: JsonObject) -> QuestionRequest:
        question = (
            params.get("message") or params.get("question") or params.get("prompt")
        )
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
        plan: CodexPermissionPlan,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> AsyncThread:
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
        plan: CodexPermissionPlan,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> AsyncThread:
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
        conversation_id: uuid.UUID | None = None,
        interactive: bool = True,
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
        # Run-level instructions are real instructions, not prompt text: they
        # join the thread's developer instructions (an accomplice's framing
        # included), which start/resume carry to the SDK.
        developer_instructions = (
            "\n\n".join(
                part
                for part in (
                    self.config.developer_instructions,
                    instructions if isinstance(instructions, str) else None,
                )
                if part
            )
            or None
        )

        permission_mode = (
            conversation.permission_mode
            if is_codex_mode(conversation.permission_mode)
            else self.config.permission_mode
        )
        # A commissioned accomplice has no human, so the one posture that needs one
        # falls back to declining at the source; every other already says who reviews.
        if not interactive and permission_mode == "user_review":
            permission_mode = "deny_all"
        plan = CODEX_PERMISSION_PLANS[permission_mode]
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

        # The run's own workspace: a fork of the project's mirror, or of the empty
        # repository when the thread is in none. `sandbox="workspace_write"` scopes
        # writes to it, so for Codex the directory is the whole of running inside a
        # project — and the workspace is what makes that boundary this run's rather
        # than everyone's checkout.
        project = await self.run_project(conversation.thread_id)
        workspace = self.octomate.workspaces.open(conversation.thread_id, project)
        run_cwd = str(workspace.path)
        # The other axis, and the operator's rather than the conversation's: what a
        # command may touch when nobody is asked. Fixed for the run, so a posture
        # never rewrites what the whole thread reaches. One answer for every thread
        # now that every thread has a workspace of its own to be scoped to.
        sandbox = Sandbox[self.config.sandbox]

        codex_thread_id: str | None = None
        with codex_logfire.span(
            "CodexTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=run_name or "codex",
            conversation_address=str(conversation_address),
        ):
            # Entered here so the tree exists before a turn is dispatched into it,
            # and a chat thread's is thrown away when the run leaves — after the
            # client is back in the pool, which is why the pool sits inside it.
            async with workspace:
                # Registered by the react node for exactly the turns whose connection
                # has the gateway; an accomplice's or a stray conversation is not
                # there. The wiring carries the kicker's own credential, so a turn
                # whose kicker is unregistered or carries no secret launches clean,
                # with the spells withheld.
                session = self.octomate.gateway.get(conversation.id)
                gateway_bearer = (
                    await self.octomate.users.secret_of(session.user_profile)
                    if session is not None
                    else None
                )
                pooled = await self.pool.acquire(
                    conversation.id, gateway_bearer=gateway_bearer
                )
                try:
                    codex_thread = pooled.thread
                    if codex_thread is None:
                        if conversation.external_id:
                            codex_thread = await self.resume_codex_thread(
                                pooled.client,
                                thread_id=conversation.external_id,
                                plan=plan,
                                base_instructions=self.config.base_instructions,
                                cwd=run_cwd,
                                developer_instructions=developer_instructions,
                                model=sdk_model,
                                model_provider=self.config.model_provider,
                                personality=personality,
                                sandbox=sandbox,
                            )
                        else:
                            codex_thread = await self.start_codex_thread(
                                pooled.client,
                                plan=plan,
                                base_instructions=self.config.base_instructions,
                                cwd=run_cwd,
                                developer_instructions=developer_instructions,
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
                    )
                    try:
                        with self.session_ingest.driving(codex_thread.id):
                            # No `sandbox=` here. The thread already carries the
                            # mode, and a turn's is sent as a whole policy built
                            # from the SDK's defaults — which would stamp
                            # `networkAccess: false` back over what the config
                            # resolved, and narrow the writable roots with it.
                            turn = await codex_thread.turn(
                                prompt_text,
                                approval_mode=plan.sdk_mode,
                                cwd=run_cwd,
                                effort=turn_effort,
                                model=sdk_model,
                                output_schema=output_schema,
                                personality=personality,
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
            cwd=Path(run_cwd),
            external_id=accumulator.thread_id or codex_thread_id,
        )
        if source_thread_message_ids:
            if recorded_run is None:
                raise RuntimeError(
                    "prompt-source bindings require a persisted Codex run"
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
