"""The Codex agent tentacle.

Driven runs share one Codex app-server client, with server requests bridged
to a human; native sessions arrive through the hook and stream routes mounted here,
into `CodexHookIngest` and `CodexTranscriptTailer`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import uuid
from collections.abc import AsyncGenerator, Generator, Sequence
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property, partial
from pathlib import Path
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    ClassVar,
    NotRequired,
    TypedDict,
    cast,
    overload,
)

import anyio
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from httpx import URL
from octomate_protocol.stream import (
    SESSION_FILE,
    STREAM_PROTOCOL,
    StreamEof,
    StreamHello,
    StreamSnapshotCursor,
    StreamWelcome,
    client_message_adapter,
)
from openai_codex import AsyncThread, AsyncTurnHandle, InputItem, SkillInput, TextInput
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.errors import MethodNotFoundError
from openai_codex.generated.v2_all import (
    BaseBranchReviewTarget,
    Personality,
    ReasoningEffort,
    ReasoningSummary,
    ReasoningSummaryValue,
    ReviewTarget,
    TurnStatus,
    UncommittedChangesReviewTarget,
)
from openai_codex.models import Notification
from pydantic import UUID7, TypeAdapter, ValidationError
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

from octomate.capabilities.harness.deferred import DeferredSuspender, Interjections
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import Claim, CodexConfig
from octomate.mcp.server import OCTOMATE_MCP_PATH
from octomate.schemas.auth import IssuedApiKey
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.commands import (
    CommandCatalog,
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandOutcome,
    CommandResult,
)
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
)
from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    DeferredActionBatch,
    QuestionRequest,
)
from octomate.schemas.files import Jsonl
from octomate.schemas.messages import ModelRequest
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import CODEX_NATIVE_ID, ThreadKey
from octomate.schemas.triage import TeleportDecision
from octomate.schemas.user import UserProfile
from octomate.streaming.files import FileTransferError, FileTransferSlot
from octomate.telemetry import (
    agent_input_message_attributes,
    codex_logfire,
)
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle
from octomate.tentacles.codex.adapter import (
    CODEX_PROVIDER_NAME,
    CodexRunAccumulator,
    json_object_adapter,
)
from octomate.tentacles.codex.catalog import APP_COMMANDS
from octomate.tentacles.codex.hooks import CodexHookInput
from octomate.tentacles.codex.ingest import CodexHookIngest
from octomate.tentacles.codex.ink import CodexInk
from octomate.tentacles.codex.schemas import CodexCommandDescriptor
from octomate.tentacles.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.codex.transcript import RolloutLine, rollout_line_adapter
from octomate.tentacles.hooks import hook_guard, hook_sender
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject
from octomate.types.permissions import PermissionMode
from octomate.utils import drain_task

if TYPE_CHECKING:
    from octomate.base import Octomate
    from octomate.managers.auth import AuthManager
    from octomate.managers.commands import CommandManager
    from octomate.managers.conversation import ConversationManager
    from octomate.managers.deferred import DeferredActionManager
    from octomate.managers.files import FileManager
    from octomate.managers.gateway import GatewayManager
    from octomate.managers.project import ProjectManager
    from octomate.managers.thread import ThreadManager
    from octomate.managers.user import UserManager
    from octomate.managers.workspaces import WorkspaceManager
    from octomate.mcp.base import KnownBearers

logger = logging.getLogger(__name__)


@dataclass
class CodexBridgeContext:
    """The driven turn a Codex server request is answered for.

    Held by native thread ID while a turn runs, so shared SDK requests reach
    the correct conversation and channel context.
    """

    conversation: Conversation
    session_allowed: set[str]
    # What a request pauses the turn on, and how its batch reaches the turn's stream.
    suspender: DeferredSuspender | None
    interjections: Interjections[Notification]
    # Captured per turn: SDK transport threads have no run context.
    task_context: Context = field(default_factory=copy_context)


# The pinned SDK generates notifications but omits this server-request shape.
class CodexInputOption(TypedDict):
    """One choice offered on a Codex user-input question."""

    label: str
    description: str


class CodexInputQuestion(TypedDict):
    """One question in a Codex user-input request, with its optional choices."""

    id: str
    header: str
    question: str
    options: NotRequired[list[CodexInputOption] | None]


class CodexInputRequest(TypedDict):
    """The params of Codex's `item/tool/requestUserInput` server request."""

    itemId: str
    questions: list[CodexInputQuestion]


codex_input_adapter = TypeAdapter(CodexInputRequest)


@dataclass
class CodexTentacle(AgentTentacle[str, None]):
    """OpenAI Codex SDK runner exposed as an Octomate agent tentacle.

    Codex owns its conversation state through SDK threads. Octomate stores the
    Codex thread id as the conversation `external_id`, then resumes that thread for
    later turns. One app-server process serves the tentacle's discovery requests
    and conversation threads. Conversations persist their native thread IDs;
    Codex reports whether each thread is loaded. Discovery does not create a
    conversation thread. The notification stream is translated into the same
    pydantic-ai event and message projections that channel feelers already render
    for other agents.
    A `teleport` cast through the Octomate MCP server
    interrupts the turn, which ends as the deferral the graph performs and resumes
    the agent from, and a resumed run opens from what the graph resolved it with.
    """

    config: CodexConfig = field(init=False)
    provider: str = field(init=False)
    ink: CodexInk = field(init=False, repr=False)
    conversations: ConversationManager = field(init=False, repr=False)
    deferred_actions: DeferredActionManager = field(init=False, repr=False)
    workspaces: WorkspaceManager = field(init=False, repr=False)
    users: UserManager = field(init=False, repr=False)
    bearers: KnownBearers = field(init=False, repr=False)
    auth: AuthManager | None = field(init=False, repr=False)
    gateway_manager: GatewayManager = field(init=False, repr=False)
    live_turns: dict[uuid.UUID, AsyncTurnHandle] = field(
        default_factory=dict, init=False
    )
    conversation_locks: SessionLocks = field(default_factory=SessionLocks, init=False)
    api_keys: dict[uuid.UUID, IssuedApiKey] = field(
        default_factory=dict, init=False, repr=False
    )  # One MCP credential per user for this runtime.
    api_key_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    bridge_contexts: dict[str, CodexBridgeContext] = field(
        default_factory=dict, init=False
    )

    # Codex approvals/questions are answered in-process through SDK callbacks while
    # the turn stays live. `pending` parks the card response futures.
    in_process: ClassVar[bool] = True
    native_id: ClassVar[str] = CODEX_NATIVE_ID

    permission_modes: tuple[PermissionMode, ...] = (
        PermissionMode(
            value="user_review",
            name="Ask for approval",
            description="Ask before editing external files or accessing the internet.",
        ),
        PermissionMode(
            value="auto_review",
            name="Approve for me",
            description="Automatically review requests to go beyond the workspace sandbox.",
        ),
        PermissionMode(
            value="full_access",
            name="Full access",
            description="Unrestricted filesystem and internet access without approval prompts.",
        ),
    )

    @property
    def default_permission_mode(self) -> str | None:
        return self.config.permission_mode

    async def apply_permission_mode(
        self, conversation: Conversation, mode: str
    ) -> None:
        # A first run may have assigned its native ID since the caller loaded it.
        current = await self.conversations.get(conversation.id, with_history=False)
        if current.external_id is None:
            return
        await self.ink.set_permission_mode(
            current.external_id,
            conversation_id=current.id,
            approval_mode=ApprovalMode.auto_review
            if mode == "auto_review"
            else ApprovalMode.deny_all
            if mode == "full_access"
            else None,
            sandbox=Sandbox.full_access
            if mode == "full_access"
            else Sandbox.workspace_write,
        )

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
        commands: CommandManager,
        projects: ProjectManager,
        threads: ThreadManager,
        files: FileManager,
        conversations: ConversationManager,
        deferred_actions: DeferredActionManager,
        workspaces: WorkspaceManager,
        users: UserManager,
        bearers: KnownBearers,
        auth: AuthManager | None,
        gateway_manager: GatewayManager,
        description: str | None = None,
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            commands=commands,
            projects=projects,
            threads=threads,
            files=files,
        )
        self.conversations = conversations
        self.deferred_actions = deferred_actions
        self.workspaces = workspaces
        self.users = users
        self.bearers = bearers
        self.auth = auth
        self.gateway_manager = gateway_manager
        self.config = config
        self.description = description or self.description
        self.api_keys = {}
        self.api_key_lock = asyncio.Lock()
        self.live_turns = {}
        self.conversation_locks = SessionLocks()
        self.bridge_contexts = {}
        self.pendings = {}
        self.claims = dict(config.claims)
        self.gateway = config.gateway
        self.models = {}
        self.provider = "openai"
        self.session_locks = SessionLocks()
        self.session_tailer = CodexTranscriptTailer(
            self.conversations,
            self.threads,
            self.session_locks,
        )
        self.session_ingest = CodexHookIngest(
            self.session_tailer,
            self.session_locks,
            conversations=self.conversations,
            projects=self.projects,
            threads=self.threads,
        )
        deployment = self.octomate.config
        self.ink = CodexInk(
            config,
            agent_id=self.id,
            mcp_url=URL(
                scheme="http",
                host="127.0.0.1"
                if deployment.host.is_unspecified
                else str(deployment.host),
                port=deployment.port,
                path=OCTOMATE_MCP_PATH,
            ),
            handler=self.handle_sdk_request,
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
        verifier = hook_guard(self.bearers)
        resolve_sender = hook_sender(self.users, self.native_id, verifier)
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
        """The attached half of a stream connection: register the session, answer
        resume offsets, then feed each framed line through the tailer's assembly.
        Codex turns close on their own `task_complete`/`turn_aborted` lines, so
        nothing commits at the boundary either way — `eof` and a drop alike just
        return the registry slot. The stored prefix rebuilds the tailer on the
        server; the client resumes after it and uploads only appended bytes. A
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
            holder = self.projects.resolve(Path(hello.cwd))
            project = self.projects.get(holder) if holder is not None else None
        await self.threads.ensure(
            ThreadKey(self.native_id, "thread", hello.session_id),
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
        conversation = state.conversation
        assert conversation is not None
        slot = FileTransferSlot(
            websocket=websocket,
            persist=(
                partial(
                    self.conversations.store_transcript,
                    conversation=conversation,
                    files=self.files,
                    owner_id=sender.user_id,
                )
                if sender.user_id is not None
                else None
            ),
            filename=Path(hello.transcript_path).name,
            content_type="application/jsonl",
            offset=offsets.get(SESSION_FILE, 0),
        )

        async def relay_finalize() -> None:
            await state.stop_event.wait()
            try:
                await slot.finalize()
            except Exception:
                # The socket died first; the drain this asked for cannot happen, and
                # the next connect re-streams what it would have shipped.
                logger.debug(
                    "session %s: finalize relay lost its socket", hello.session_id
                )

        relay: asyncio.Task[None] | None = None
        clean = False
        try:
            if conversation.transcript_file_id is not None:
                if sender.user_id is None:
                    raise FileTransferError("Stored transcripts require an owner")
                slot.prefix = await self.files.read(
                    conversation.transcript_file_id, owner_id=sender.user_id
                )
            for raw in slot.prefix.split(b"\n")[:-1]:
                end = slot.offset + len(raw) + 1
                await self.session_tailer.feed_remote(
                    state, None, raw.decode("utf-8", errors="replace"), slot.offset, end
                )
                slot.offset = end
            slot.stored_offset = len(slot.prefix)
            offsets[SESSION_FILE] = slot.offset
            await websocket.send_text(
                StreamWelcome(
                    offsets=offsets,
                    transcript=StreamSnapshotCursor(
                        offset=len(slot.prefix),
                        sha256=hashlib.sha256(slot.prefix).hexdigest(),
                    )
                    if slot.persist is not None
                    else None,
                ).model_dump_json()
            )
            relay = asyncio.create_task(relay_finalize())
            # Each line must begin where the preceding line ended.
            expected_offsets = dict(offsets)
            while True:
                message = await slot.receive()
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
        except (ValidationError, FileTransferError, FileNotFoundError):
            await websocket.close(code=1008, reason="invalid stream message")
        except Exception:
            logger.exception(
                "session %s: remote tail errored; its open turns are left for the "
                "next connect to re-stream",
                hello.session_id,
            )
        finally:
            if relay is not None:
                relay.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await relay
            await slot.close()
            self.session_tailer.detach_remote(state)
            if clean:
                with contextlib.suppress(Exception):
                    await websocket.close()

    async def fork_session(
        self, conversation: Conversation, *, cwd: Path, last_turn_id: str | None = None
    ) -> str:
        """Copy Codex's stored history into an independent, durable thread."""
        if not conversation.external_id:
            raise ValueError("Cannot fork a Codex conversation without a session id")
        return await self.ink.fork_thread(
            conversation.external_id, cwd=cwd, last_turn_id=last_turn_id
        )

    async def read_fork_transcript(
        self, source: Conversation, *, owner_id: UUID7
    ) -> tuple[bytes, ExternalAgentRun]:
        """Read uploaded history and select its latest fully uploaded terminal turn."""
        if source.agent_tentacle_id != self.native_id or source.subagent_id:
            raise ValueError("Only root native Codex sessions can be forked here")
        if source.transcript_file_id is None or source.external_id is None:
            raise ValueError("Native Codex history has not been uploaded")
        data = await self.files.read(source.transcript_file_id, owner_id=owner_id)
        end = 0
        completed_run: ExternalAgentRun | None = None
        for run in source.runs:
            if (
                not isinstance(run, ExternalAgentRun)
                or run.native_session_id != source.external_id
                or run.end_offset is None
                or not end < run.end_offset <= len(data)
            ):
                continue
            offset = run.end_offset
            if data[offset - 1 : offset] != b"\n":
                raise ValueError("Turn offset must end at a transcript line boundary")
            start = data.rfind(b"\n", 0, offset - 1) + 1
            closing = rollout_line_adapter.validate_json(data[start:offset])
            if (
                closing.type == "event_msg"
                and closing.payload.get("type") in {"task_complete", "turn_aborted"}
                and closing.payload.get("turn_id") == run.id
            ):
                end = offset
                completed_run = run
        if completed_run is None:
            raise ValueError("No completed Codex turn has been fully uploaded")
        if completed_run.permission_mode is None:
            raise ValueError(
                "The completed Codex turn has no supported permission preset"
            )
        # Uploads may stop inside the next record; Codex selects the turn boundary.
        data = data[: data.rfind(b"\n") + 1]
        self.import_metadata(data, external_id=source.external_id)
        return data, completed_run

    async def fork_transcript(
        self,
        source: Conversation,
        target: Conversation,
        *,
        owner_id: UUID7,
        cwd: Path,
    ) -> Conversation:
        """Import an owner's completed native history and fork it into an empty target."""
        conversations = self.conversations
        async with conversations.lock(target.key):
            source = await conversations.get(source.id)
            target = await conversations.get(target.id)
            if source.agent_tentacle_id != CODEX_NATIVE_ID:
                raise ValueError("Transcript imports require a native Codex source")
            if target.agent_tentacle_id != self.id or source.id == target.id:
                raise ValueError("Transcript imports require a separate Codex target")
            if target.messages or target.external_id or target.transcript_file_id:
                raise ValueError("Transcript imports require an empty target")
            if source.transcript_file_id is None or source.external_id is None:
                raise ValueError("Native Codex history has not been uploaded")
            data, completed_run = await self.read_fork_transcript(
                source, owner_id=owner_id
            )
            assert completed_run.end_offset is not None
            async with (
                self.files.partial_copy(
                    source.transcript_file_id,
                    end=completed_run.end_offset,
                    owner_id=owner_id,
                    media_type="application/jsonl",
                ) as snapshot,
                self.import_transcript(
                    data, external_id=source.external_id, owner_id=owner_id
                ) as imported_id,
            ):
                imported_source = Conversation(
                    thread_id=source.thread_id,
                    agent_tentacle_id=self.id,
                    external_id=imported_id,
                )
                external_id = await self.fork_session(
                    imported_source, cwd=cwd, last_turn_id=completed_run.id
                )
                await conversations.fork(
                    source,
                    target,
                    external_id=external_id,
                    transcript=Jsonl.model_validate(snapshot),
                    model_name=completed_run.model_name,
                    permission_mode=completed_run.permission_mode,
                )
            return await conversations.get(target.id)

    @staticmethod
    def import_metadata(data: bytes, *, external_id: str) -> RolloutLine:
        """Require a complete snapshot with its own matching session metadata."""
        if not data.endswith(b"\n"):
            raise ValueError("Completed turn must end at a transcript line boundary")
        opening = data.split(b"\n", 1)[0]
        metadata = rollout_line_adapter.validate_json(opening)
        if metadata.type != "session_meta" or metadata.payload.get("id") != external_id:
            raise ValueError("Transcript metadata does not match the source session")
        if metadata.payload.get("history_base") is not None:
            raise ValueError(
                "This transcript requires ancestor files before it can be imported"
            )
        return metadata

    @contextlib.asynccontextmanager
    async def import_transcript(
        self, data: bytes, *, external_id: str, owner_id: UUID7
    ) -> AsyncGenerator[str]:
        """Keep a private Codex import on success; remove it if the fork fails."""
        metadata = self.import_metadata(data, external_id=external_id)
        _, remainder = data.split(b"\n", 1)
        # Distinct imports of A must not compete for A's ID in Codex's rollout index.
        imported_id = str(uuid7())
        metadata.payload["id"] = imported_id
        metadata.payload["session_id"] = imported_id
        imported = metadata.model_dump_json().encode() + b"\n" + remainder
        name = f"rollout-{metadata.timestamp:%Y-%m-%dT%H-%M-%S}-{imported_id}.jsonl"
        home = await anyio.Path(
            (self.config.runtime.env or {}).get("CODEX_HOME")
            or os.environ.get("CODEX_HOME")
            or Path.home() / ".codex"
        ).expanduser()
        path = await (home / "sessions" / "users" / owner_id.hex / name).absolute()
        await path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = await path.open("xb")
        try:
            async with handle:
                await handle.write(imported)
            await path.chmod(0o600)
            yield imported_id
        except BaseException:
            await path.unlink(missing_ok=True)
            raise

    async def probe_commands(self, context: CommandContext) -> CommandCatalog:
        """List app actions and inspect skills without preparing a workspace or turn."""
        conversation = context.conversation
        bound = (
            conversation is not None
            and conversation.external_id is not None
            and (binding := self.ink.thread_bindings.get(conversation.external_id))
            is not None
            and binding[0] == conversation.id
        )
        catalog = CommandCatalog(
            context=context,
            status="ready",
            descriptors={
                descriptor.model_copy(
                    update={
                        "unavailable_reason": "Run this conversation in Codex before using this command."
                    }
                )
                if descriptor.name in {"plan", "mcp", "compact"} and not bound
                else descriptor
                for descriptor in APP_COMMANDS
            },
        )
        if context.cwd is None or not await anyio.Path(context.cwd).is_dir():
            catalog.limitations.append(
                "Skills are discoverable after a conversation workspace exists."
            )
            return catalog
        try:
            entry = await self.ink.skills(context.cwd)
        except MethodNotFoundError:
            catalog.limitations.append(
                "This Codex runtime does not support skill discovery."
            )
            return catalog
        catalog.descriptors.update(
            CodexCommandDescriptor(
                id=f"skill:{skill.path.root}",
                name=skill.name,
                description=skill.description,
                path=Path(skill.path.root),
                scope=skill.scope,
                plugin_id=skill.plugin_id,
            )
            for skill in entry.skills
            if skill.enabled
        )
        catalog.limitations.extend(
            f"{error.path}: {error.message}" for error in entry.errors
        )
        return catalog

    async def execute_command(
        self,
        context: CommandContext,
        invocation: CommandInvocation,
        *,
        deferred_suspender: DeferredSuspender | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> AsyncGenerator[CommandOutcome | ReactStreamEvent[str], None]:
        """Dispatch controls directly and stream only commands that start a run."""
        conversation = context.conversation
        catalog = await self.discover_commands(context)
        descriptors = {descriptor.id: descriptor for descriptor in catalog.descriptors}
        descriptor = descriptors.get(invocation.command_id)
        if descriptor is None:
            yield CommandError(
                status="stale",
                message="This command is no longer available; refresh commands.",
            )
            return
        if descriptor.id in {
            "builtin:status",
            "builtin:mcp",
            "builtin:plan",
            "builtin:reasoning",
            "builtin:compact",
        }:
            yield await self.command_control(context, invocation)
            return
        if conversation is None:
            raise ValueError("an agent command run requires a conversation")
        review: ReviewTarget | None = None
        if isinstance(descriptor, CodexCommandDescriptor):
            inputs: list[InputItem] = [
                SkillInput(name=descriptor.name, path=str(descriptor.path))
            ]
            prompt = f"/{descriptor.name}"
            if invocation.arguments:
                inputs.append(TextInput(invocation.arguments))
                prompt += f" {invocation.arguments}"
        elif descriptor.id == "builtin:init":
            if invocation.arguments.strip():
                yield CommandError(
                    status="unsupported", message="/init takes no arguments."
                )
                return
            prompt = (
                "Inspect this workspace and create or update AGENTS.md with concise, "
                "accurate instructions for agents working in this project. Use the "
                "repository's actual build, test and style conventions. Preserve "
                "existing project instructions that remain applicable."
            )
            inputs = [TextInput(prompt)]
        elif descriptor.id == "builtin:review":
            branch = invocation.arguments.strip()
            if branch and (
                branch.startswith("-") or any(char.isspace() for char in branch)
            ):
                yield CommandError(
                    status="unsupported", message="Use /review [branch]."
                )
                return
            review = ReviewTarget(
                BaseBranchReviewTarget(type="baseBranch", branch=branch)
                if branch
                else UncommittedChangesReviewTarget(type="uncommittedChanges")
            )
            prompt = f"/review {branch}" if branch else "/review"
            inputs = []
        else:
            yield CommandError(
                status="unavailable",
                message=descriptor.unavailable_reason
                or "This command is not supported.",
            )
            return
        selected_model = self.resolve_model(context.model)
        async with contextlib.aclosing(
            self.observe_run(
                self._iter_events(
                    prompt,
                    native_input=inputs,
                    review=review,
                    conversation_address=context.address,
                    thread_id=conversation.thread_id,
                    conversation_id=conversation.id,
                    run_name=descriptor.name,
                    model=self.models[selected_model]
                    if selected_model is not None
                    else None,
                    deferred_suspender=deferred_suspender,
                    capabilities=capabilities,
                )
            )
        ) as events:
            async for event in events:
                yield event

    async def command_control(
        self, context: CommandContext, invocation: CommandInvocation
    ) -> CommandOutcome:
        """Apply native controls or Octomate-owned effort without a model run."""
        argument = invocation.arguments.strip()
        conversation = context.conversation
        if invocation.command_id == "builtin:status":
            if argument:
                return CommandError(
                    status="unsupported", message="/status takes no arguments."
                )
            effort = (conversation.effort if conversation else None) or (
                self.config.effort.value if self.config.effort is not None else None
            )
            text = (
                f"Agent: {self.id}\nModel: {context.model or self.default_model or 'runtime default'}\n"
                f"Permissions: {context.permission_mode or self.default_permission_mode}\n"
                f"Effort: {effort or 'runtime default'}\n"
                f"Conversation: {conversation.id if conversation else 'not created'}\n"
                f"Workspace: {context.cwd or 'not created'}"
            )
            return CommandResult(segments=[TextSegment(data={"text": text})])
        if conversation is None:
            raise ValueError("this command requires a conversation")
        if invocation.command_id == "builtin:reasoning":
            effort = argument or None
            if effort is not None:
                try:
                    self.check_effort(context.model, effort)
                except ValueError as error:
                    return CommandError(status="unsupported", message=str(error))
            await self.conversations.set_effort(conversation, effort)
            text = f"Reasoning effort: {effort or 'default'}."
            return CommandResult(segments=[TextSegment(data={"text": text})])
        thread_id = conversation.external_id
        binding = self.ink.thread_bindings.get(thread_id) if thread_id else None
        if thread_id is None or binding is None or binding[0] != conversation.id:
            return CommandError(
                status="unavailable",
                message="Run this conversation in Codex before using this command.",
            )
        if invocation.command_id == "builtin:compact":
            if argument:
                return CommandError(
                    status="unsupported", message="/compact takes no arguments."
                )
            project = await self.run_project(conversation.thread_id)
            async with (
                self.conversation_locks.hold(str(conversation.id)),
                self.workspaces.open(conversation.thread_id, project),
                self.driving(thread_id),
            ):
                turn = await self.ink.start_command(thread_id)
                completed = await turn.run()
            if completed.status != TurnStatus.completed:
                return CommandError(
                    status="failed",
                    message=completed.error.message
                    if completed.error
                    else f"Codex compaction {completed.status.value}.",
                )
            return CommandResult(
                segments=[TextSegment(data={"text": "Conversation context compacted."})]
            )
        if invocation.command_id == "builtin:mcp":
            if argument:
                return CommandError(
                    status="unsupported", message="/mcp takes no arguments."
                )
            text = await self.ink.mcp_status(thread_id)
            return CommandResult(segments=[TextSegment(data={"text": text})])
        if argument not in {"", "on", "off"}:
            return CommandError(status="unsupported", message="Use /plan [on|off].")
        selected = self.resolve_model(context.model or self.default_model)
        if selected is None:
            return CommandError(
                status="unavailable",
                message="Select a model before changing planning mode.",
            )
        model = self.models[selected]
        effort = await self.resolve_effort(conversation, model=selected)
        await self.ink.set_plan_mode(
            thread_id,
            enabled=argument != "off",
            model=model.model_name if isinstance(model, Model) else model,
            effort=ReasoningEffort(effort)
            if effort is not None
            else self.config.effort,
        )
        text = f"Planning mode {'disabled' if argument == 'off' else 'enabled'} for subsequent turns."
        return CommandResult(segments=[TextSegment(data={"text": text})])

    async def runtime_api_key(self, user_id: uuid.UUID | None) -> IssuedApiKey | None:
        """Reuse one MCP key per user, replacing it when its lifetime expires."""
        auth = self.auth
        if user_id is None or auth is None:
            return None
        async with self.api_key_lock:
            if not self.ink.running:
                raise RuntimeError("Codex runtime is not running")
            issued = self.api_keys.get(user_id)
            if issued is not None and (
                issued.key.expires_at is None
                or issued.key.expires_at > datetime.now(UTC)
            ):
                return issued
            with anyio.CancelScope(shield=True):
                if issued is not None:
                    await auth.revoke_api_key(user_id, issued.key.id)
                issued = await auth.create_api_key(
                    user_id,
                    name=f"Codex {self.id}",
                    scopes=["mcp"],
                    expires_at=datetime.now(UTC) + auth.config.runtime_api_key_lifetime,
                )
                self.api_keys[user_id] = issued
            return issued

    async def revoke_runtime_keys(self) -> None:
        """Revoke user credentials at tentacle shutdown."""
        async with self.api_key_lock:
            api_keys, self.api_keys = self.api_keys, {}
            auth = self.auth
            if auth is None:
                return
            for issued in api_keys.values():
                await auth.revoke_api_key(issued.key.user_id, issued.key.id)

    async def discover_models(self) -> None:
        catalog = await self.ink.models()
        models: dict[str, Model | str] = {}
        claims: dict[str, Claim] = {}
        for model in catalog.models:
            key = f"{catalog.provider}:{model.model}"
            configured = self.config.claims.get(key)
            efforts = tuple(
                option.reasoning_effort.value
                for option in model.supported_reasoning_efforts
            )
            default = (
                self.config.effort
                or catalog.configured_effort
                or model.default_reasoning_effort
            ).value
            models[key] = model.model
            claims[key] = Claim(
                model.description
                or (configured.ability if configured else model.display_name),
                efforts,
                next((effort for effort in efforts if effort == default), None),
            )
        self.set_model_catalog(models, claims)
        self.default_model = (
            f"{catalog.provider}:{catalog.default_model}"
            if catalog.default_model is not None
            else None
        )
        self.provider = catalog.provider

    async def __aenter__(self) -> CodexTentacle:
        await self.ink.start(
            invalidate_commands=lambda agent_id: self.commands.invalidate(
                agent_id=agent_id
            )
        )
        try:
            await self.discover_models()
            return await super().__aenter__()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.__aexit__()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await super().__aexit__(exc_type, exc_value, traceback)
        with anyio.CancelScope(shield=True):
            cancelled = await drain_task(asyncio.gather(*self.run_tasks))
            await self.session_tailer.shutdown()
            await self.ink.close()
            self.bridge_contexts.clear()
            for future in list(self.pendings.values()):
                if not future.done():
                    future.cancel()
            self.pendings.clear()
            await self.revoke_runtime_keys()
        if cancelled:
            raise asyncio.CancelledError

    async def _await_human(
        self,
        *,
        context: CodexBridgeContext,
        requests: DeferredToolRequests,
    ) -> tuple[DeferredActionBatch, DeferredActionBatchResponse | None]:
        if context.suspender is None:
            raise RuntimeError(
                "a Codex approval mid-turn needs a suspender to pause on"
            )
        # Waiting before the cards go up, so a quick answer cannot miss it.
        batch_id: UUID7 = uuid7()
        future: asyncio.Future[DeferredActionBatchResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pendings[batch_id] = future
        try:
            batch, event = await context.suspender.pause(requests, batch_id=batch_id)
            if event is not None:
                context.interjections.interject(event)
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

    async def handle_sdk_request(
        self, method: str, params: JsonObject | None
    ) -> JsonObject:
        thread_id = (params or {}).get("threadId")
        context = (
            self.bridge_contexts.get(thread_id) if isinstance(thread_id, str) else None
        )
        if context is None:
            if method == "item/tool/requestUserInput":
                return {"answers": {}}
            return self.deny_sdk_request(f"Octomate has no live context for {method}.")
        if method == "item/tool/requestUserInput":
            answer = self.answer_sdk_user_input_request(context=context, params=params)
        elif self.is_mcp_tool_approval(method, params):
            answer = self.answer_sdk_mcp_tool_approval(
                context=context, params=params or {}
            )
        elif self.is_approval_request(method):
            answer = self.answer_sdk_approval_request(
                context=context, method=method, params=params
            )
        elif self.is_question_request(method):
            answer = self.answer_sdk_question_request(
                context=context, method=method, params=params
            )
        else:
            logger.warning("Unhandled Codex server request: %s", method)
            return {}
        return await asyncio.create_task(answer, context=context.task_context.copy())

    async def answer_sdk_user_input_request(
        self,
        *,
        context: CodexBridgeContext,
        params: JsonObject | None,
    ) -> JsonObject:
        """Present Codex's choices, including MCP consent, without choosing for
        the human. The reply must name Codex's question ids, not our card ids."""
        request = codex_input_adapter.validate_python(params)
        questions: list[QuestionRequest] = []
        for question in request["questions"]:
            options = (question.get("options") or [])[:MAX_QUESTION_CHOICES]
            questions.append(
                QuestionRequest(
                    question=question["question"],
                    choices=[option["label"] for option in options] or None,
                    hint="\n".join(
                        [
                            question["header"],
                            *(
                                f"{option['label']}: {option['description']}"
                                for option in options
                            ),
                        ]
                    ),
                )
            )
        batch, response = await self._await_human(
            context=context,
            requests=DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="codex_user_input",
                        tool_call_id=request["itemId"],
                        args={"questions": questions},
                        provider_name=CODEX_PROVIDER_NAME,
                    )
                ]
            ),
        )
        answers: JsonObject = {}
        if response is not None:
            for action in batch.questions:
                answer = response.answers.get(action.id)
                if answer:
                    question_id = request["questions"][action.position]["id"]
                    answers[question_id] = {
                        "answers": [answer] if isinstance(answer, str) else [*answer]
                    }
        return {"answers": answers}

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
            await self.conversations.grant_session_tool(
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

    async def answer_sdk_mcp_tool_approval(
        self, *, context: CodexBridgeContext, params: JsonObject
    ) -> JsonObject:
        """Codex asking to run an MCP tool, as an approve-or-decline card in Codex's
        own words, which are the only place it names the tool, showing the
        arguments the call would run with."""
        server = str(params.get("serverName") or "mcp")
        tool_call_id = self.sdk_request_id("mcpServer/elicitation/request", params)
        message = params.get("message")
        meta = params.get("_meta")
        arguments = meta.get("tool_params") if isinstance(meta, dict) else None
        requests = DeferredToolRequests(
            approvals=[
                ToolCallPart(
                    tool_name=f"codex_mcp_{server}",
                    args=arguments if isinstance(arguments, dict) else {},
                    tool_call_id=tool_call_id,
                    provider_name=CODEX_PROVIDER_NAME,
                )
            ],
            metadata={tool_call_id: {"description": message}}
            if isinstance(message, str)
            else {},
        )
        batch, response = await self._await_human(context=context, requests=requests)
        action = next(iter(batch.approvals))
        if response is not None and response.approvals.get(action.id, False):
            return {"action": "accept", "content": {}}
        return {"action": "decline"}

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
        answer = "\n".join(
            item if isinstance(item, str) else ", ".join(item)
            for item in answers
            if item
        )
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
    def is_mcp_tool_approval(method: str, params: JsonObject | None) -> bool:
        """Codex's approval prompt for an MCP tool call, which it sends as an
        elicitation marked with the kind of approval it is."""
        if method != "mcpServer/elicitation/request" or params is None:
            return False
        meta = params.get("_meta")
        return (
            isinstance(meta, dict)
            and meta.get("codex_approval_kind") == "mcp_tool_call"
        )

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

    @contextlib.contextmanager
    def track_turn(
        self, conversation_id: UUID7, turn: AsyncTurnHandle
    ) -> Generator[None]:
        self.live_turns[conversation_id] = turn
        try:
            yield
        finally:
            if self.live_turns.get(conversation_id) is turn:
                self.live_turns.pop(conversation_id, None)

    async def sync_session_name(
        self, conversation: Conversation, codex_thread: AsyncThread
    ) -> None:
        name = await self.ink.thread_name(codex_thread)
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
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
        native_input: list[InputItem] | None = None,
        review: ReviewTarget | None = None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        sdk_model = model.model_name if isinstance(model, Model) else model
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
        if not self.ink.running:
            raise RuntimeError("CodexTentacle.run requires the tentacle to be entered")
        if output_type is DeferredToolRequests or (
            isinstance(output_type, (list, tuple))
            and DeferredToolRequests in output_type
        ):
            raise ValueError(
                "CodexTentacle does not support DeferredToolRequests in output_type"
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
        if deferred_tool_results is not None:
            # A resumed run. The app-server takes no tool result back, so the graph's
            # resolution of the deferral is this turn's prompt, ledgered as one.
            user_prompt = self.resumed_prompt(deferred_tool_results)
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

        personality = (
            Personality(self.config.personality)
            if self.config.personality is not None
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
        workspace = self.workspaces.open(conversation.thread_id, project)
        run_cwd = str(workspace.path)

        async with contextlib.AsyncExitStack() as resources:
            resources.enter_context(
                codex_logfire.span(
                    "CodexTentacle {agent_id} {run_name} [{conversation_address}]",
                    agent_id=self.id,
                    run_name=run_name or "codex",
                    conversation_address=str(conversation_address),
                    **agent_input_message_attributes(user_prompt),
                )
            )
            await resources.enter_async_context(
                self.conversation_locks.hold(str(conversation.id))
            )
            conversation = await self.conversations.get(
                conversation.id, with_history=False
            )
            effort = await self.resolve_effort(
                conversation, model=sdk_model, effort=effort
            )
            turn_effort = (
                ReasoningEffort(effort) if effort is not None else self.config.effort
            )
            permission_mode = (
                conversation.permission_mode or self.config.permission_mode
            )
            self.check_permission_mode(permission_mode)
            sandbox = (
                Sandbox.full_access
                if permission_mode == "full_access"
                else Sandbox.workspace_write
            )
            approval_mode = (
                ApprovalMode.auto_review
                if permission_mode == "auto_review"
                else ApprovalMode.deny_all
                if permission_mode == "full_access" or not interactive
                else None
            )
            # Keep the workspace alive until the native turn finishes.
            await resources.enter_async_context(workspace)
            self.commands.invalidate(agent_id=self.id, conversation_id=conversation.id)
            session = self.gateway_manager.get(conversation.id)
            user_id = (
                session.user_profile.user_id
                if session is not None and session.user_profile is not None
                else None
            )
            api_key = await self.runtime_api_key(user_id)
            codex_thread, model_name = await self.ink.open_thread(
                thread_id=conversation.external_id,
                conversation_id=conversation.id,
                api_key_id=api_key.key.id if api_key is not None else None,
                mcp_bearer=api_key.token if api_key is not None else None,
                cwd=run_cwd,
                approval_mode=approval_mode,
                base_instructions=self.config.base_instructions,
                developer_instructions=developer_instructions,
                ephemeral=self.config.ephemeral,
                model=sdk_model,
                model_provider=self.provider if sdk_model is not None else None,
                personality=personality,
                sandbox=sandbox,
            )
            codex_thread_id = codex_thread.id
            if conversation.external_id != codex_thread_id:
                await self.conversations.set_external_id(conversation, codex_thread_id)
            await resources.enter_async_context(self.driving(codex_thread_id))
            interjections = Interjections[Notification]()
            self.bridge_contexts[codex_thread_id] = CodexBridgeContext(
                conversation=conversation,
                session_allowed=set(conversation.allowed_tools),
                suspender=deferred_suspender,
                interjections=interjections,
            )
            resources.callback(self.bridge_contexts.pop, codex_thread_id, None)
            turn = await self.ink.start_turn(
                codex_thread,
                native_input if native_input is not None else prompt_text,
                approval_mode=approval_mode,
                sandbox=sandbox,
                cwd=run_cwd,
                effort=turn_effort,
                model=sdk_model,
                output_schema=output_schema,
                personality=personality,
                summary=summary,
                review=review,
            )
            resources.enter_context(self.track_turn(conversation.id, turn))
            # Closed with the turn's other resources, so its reader never outlives it.
            notifications = await resources.enter_async_context(
                contextlib.aclosing(interjections.around(turn.stream()))
            )
            interrupted = False
            async for notification in notifications:
                if isinstance(notification, ActionBatchEvent):
                    # A batch the turn paused on, for whoever draws the run.
                    yield notification
                    continue
                for event in accumulator.consume(notification):
                    yield event
                if (
                    not interrupted
                    and session is not None
                    and isinstance(session.decision, TeleportDecision)
                ):
                    # Stop here so the graph can move and resume the conversation.
                    interrupted = True
                    await turn.interrupt()
            await self.sync_session_name(conversation, codex_thread)

        run_id = str(uuid7())
        recorded_run = await self.conversations.record_agent_run(
            conversation,
            run_id=run_id,
            messages=accumulator.messages,
            name=run_name,
            model_name=model_name,
            permission_mode=permission_mode,
            cwd=Path(run_cwd),
            external_id=codex_thread_id,
            native_id=CODEX_NATIVE_ID,
            native_turn_id=accumulator.turn_id,
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
        if accumulator.turn_status == TurnStatus.failed:
            raise AgentRunError(accumulator.turn_error or "Codex turn failed")
        moving = session.decision if session is not None else None
        if isinstance(moving, TeleportDecision):
            if deferred_suspender is None:
                raise RuntimeError(
                    "a teleport mid-run needs a suspender to end the turn through"
                )
            requests = moving.deferral(str(uuid7()))
            await deferred_suspender.suspend(requests)
            # The deferral rides the str-typed stream as a structured result does.
            yield AgentRunResultEvent(
                cast(
                    "AgentRunResult[str]",
                    accumulator.build_deferred_result(
                        requests, run_id=run_id, conversation_id=str(conversation.id)
                    ),
                )
            )
            return
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
                deferred_tool_results=deferred_tool_results,
                deferred_suspender=deferred_suspender,
            )
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
                    deferred_tool_results=deferred_tool_results,
                    deferred_suspender=deferred_suspender,
                )
            )
        )
