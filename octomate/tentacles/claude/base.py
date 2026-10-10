"""The Claude Code agent tentacle.

Driven runs go over the Claude Agent SDK; native sessions arrive through the hook
and stream routes mounted here, into `ClaudeHookIngest` and `ClaudeTranscriptTailer`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from io import BytesIO
from pathlib import Path
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    ClassVar,
    cast,
    get_args,
    overload,
)

import anyio
from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    PreToolUseHookInput,
    ResultMessage,
    ToolPermissionContext,
)
from claude_agent_sdk.types import Message, SystemPromptPreset
from fastapi import APIRouter, Depends, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from octomate_protocol.gateway import GatewayTool, gateway_tool
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
from pydantic import UUID7, TypeAdapter, ValidationError
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
from rich.style import Style
from starlette.datastructures import Headers
from uuid_utils.compat import uuid7

from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.deferred import DeferredSuspender, Interjections
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import Claim, ClaudeCodeConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.mcp.server import OCTOMATE_SERVER_NAME, octomate_instructions
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
from octomate.schemas.messages import ModelRequest
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import CLAUDE_NATIVE_ID, ThreadKey
from octomate.schemas.triage import TeleportDecision
from octomate.schemas.user import UserProfile
from octomate.telemetry import (
    agent_input_message_attributes,
    claude_logfire,
    octomate_trace_environment,
)
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle
from octomate.tentacles.channel import ChannelTentacle
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.catalog import (
    CONTROL_COMMANDS,
    HOST_COMMANDS,
    ClaudeCommandDescriptor,
    claude_effort_adapter,
)
from octomate.tentacles.claude.hooks import ClaudeHookInput
from octomate.tentacles.claude.ingest import ClaudeHookIngest
from octomate.tentacles.claude.ink import ClaudeInk
from octomate.tentacles.claude.mcp import octomate_mcp_server
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.claude.transcript import relocate_session, transcripts_dir
from octomate.tentacles.hooks import hook_guard, hook_sender
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject
from octomate.types.permissions import (
    ClaudePermissionMode,
    PermissionMode,
    is_claude_mode,
)

if TYPE_CHECKING:
    from octomate.base import Octomate
    from octomate.managers.commands import CommandManager
    from octomate.managers.conversation import ConversationManager
    from octomate.managers.deferred import DeferredActionManager
    from octomate.managers.files import FileManager
    from octomate.managers.mcp import McpManager
    from octomate.managers.project import ProjectManager
    from octomate.managers.thread import ThreadManager
    from octomate.managers.user import UserManager
    from octomate.managers.workspaces import WorkspaceManager
    from octomate.mcp.base import KnownBearers

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeTentacle(AgentTentacle[str, None]):
    """Claude Agent SDK runner exposed as an Octomate agent tentacle.

    A run drives a `ClaudeSDKClient` over a local subprocess, translating its
    message stream through `ClaudeRunAccumulator` into live
    stream events (proxied to the channel feelers) and persisted
    `ModelMessage`s. The Claude session id is stored on the conversation as
    `external_id` and replayed via `resume=` so Claude owns its own
    context across turns. Output is the run's final text (`str`); pydantic-ai
    run options that don't map onto Claude (custom output_type, toolsets,
    capabilities, ...) are ignored — except a `GatewayCapability`, which mounts
    the unified Octomate server as the turn's in-process MCP server. A `teleport` cast through
    it interrupts the turn, which ends as the deferral the graph performs and
    resumes the agent from — in a sub-thread, a crossing, or a project's
    workspace — and a resumed run opens from what the graph resolved it with.
    """

    config: ClaudeCodeConfig = field(init=False)
    ink: ClaudeInk = field(init=False, repr=False)
    conversations: ConversationManager = field(init=False, repr=False)
    deferred_actions: DeferredActionManager = field(init=False, repr=False)
    workspaces: WorkspaceManager = field(init=False, repr=False)
    users: UserManager = field(init=False, repr=False)
    bearers: KnownBearers = field(init=False, repr=False)
    mcp: McpManager = field(init=False, repr=False)
    native_id: ClassVar[str] = CLAUDE_NATIVE_ID
    # Claude Code's own names for models, such as `opus`, to the catalog entry each
    # runs now, so a pin saved under one still finds its model.
    model_aliases: dict[str, str] = field(init=False)

    # A Claude run stays live in-process; `pending` (from `AgentTentacle`) parks a
    # waiter per gated tool / question until `Octomate.kick` delivers the response.
    in_process: ClassVar[bool] = True

    permission_modes: tuple[PermissionMode, ...] = tuple(
        PermissionMode(value=mode, name=mode) for mode in get_args(ClaudePermissionMode)
    )

    @property
    def default_permission_mode(self) -> str | None:
        return self.config.permission_mode

    async def apply_permission_mode(
        self, conversation: Conversation, mode: str
    ) -> None:
        if not is_claude_mode(mode):
            raise ValueError(f"{mode!r} is not a Claude permission mode")
        await self.ink.set_permission_mode(conversation.id, mode)

    # Claude's own orange, so its lines carry its identity in a console shared with
    # every other tentacle, instead of whatever hue the connection order landed on.
    brand_color: ClassVar[Style | None] = Style(color="#D97757", bold=True)

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
        commands: CommandManager,
        projects: ProjectManager,
        threads: ThreadManager,
        files: FileManager,
        conversations: ConversationManager,
        deferred_actions: DeferredActionManager,
        workspaces: WorkspaceManager,
        users: UserManager,
        bearers: KnownBearers,
        mcp: McpManager,
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
        self.mcp = mcp
        self.config = config
        self.description = description or self.description
        self.pendings = {}
        self.claims = dict(config.claims)
        self.gateway = config.gateway
        self.ink = ClaudeInk()
        self.models = {}
        self.model_aliases = {}
        # Per-session locks shared by the hook ingest and the transcript tailer, so a
        # session's ledger writes (hooks) and run commits (tailer) serialize.
        self.session_locks = SessionLocks()
        # Assembles native sessions' turns from streamed transcript lines — the
        # stream is the only assembler; the server never opens a transcript.
        self.session_tailer = ClaudeTranscriptTailer(
            self.conversations,
            self.threads,
            self.session_locks,
        )
        # Live hook ingest: writes the human ledger and relays the stream's drains.
        self.session_ingest = ClaudeHookIngest(
            self.session_tailer,
            self.session_locks,
            conversations=self.conversations,
            projects=self.projects,
            threads=self.threads,
        )

    def routers(self) -> tuple[APIRouter]:
        """The tentacle's HTTP surface, mounted by `Octomate.connect`: the hook router
        native Claude clients (app / CLI / VSCode) POST their session events into, and
        the stream endpoint every session's client-side tail feeds raw transcript
        lines through (`octomate claude tail`) — the server never reads a transcript
        from disk, this machine's sessions included. `octomate claude hooks install`
        writes the client-side settings that point a session at both.
        """
        return (self.hook_router,)

    @cached_property
    def hook_router(self) -> APIRouter:
        """The routes behind `routers()`; cached so they are built once. The guard
        covers the websocket too: FastAPI runs router dependencies at the handshake,
        so a bad bearer is denied with the same 401 before any socket opens. Each
        route takes `hook_sender` — the verified bearer resolved to their own
        profile, on the guard's single per-request check — as the ledger's
        principal."""
        verifier = hook_guard(self.bearers)
        resolve_sender = hook_sender(self.users, self.native_id, verifier)
        router = APIRouter(tags=["claude"], dependencies=[Depends(verifier)])

        @router.post(
            "/hooks/claude",
            summary="Claude Code hook pipe — streams a native session's human ledger in",
        )
        async def receive_hook(
            event: ClaudeHookInput,
            # `param: T = Depends(dep)` is FastAPI's own dependency contract;
            # ruff's B008 exemption misses it when T is a custom class (it is
            # fine with `str`), so the rule bends rather than the checked type.
            sender: UserProfile = Depends(resolve_sender),  # noqa: B008
        ) -> JSONResponse:
            await self.session_ingest.handle(event, sender)
            teleport = gateway_tool(GatewayTool.TELEPORT)
            if (
                event.hook_event_name == "PreToolUse"
                and event.tool_input is not None
                and (event.tool_name or "").endswith(f"__{teleport}")
            ):
                # The served teleport finds the session's history by this id.
                return JSONResponse(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "updatedInput": {
                                **event.tool_input,
                                "session_id": event.session_id,
                            },
                        }
                    }
                )
            # Claude Code reads the JSON body as the hook's decision; an empty object
            # decides nothing, which is what an observer should do.
            return JSONResponse({})

        @router.websocket("/hooks/claude/stream")
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
        resume offsets, then feed each framed line through the tailer's assembly until
        `eof` (commit the trailing turns) or a drop (leave them for the next connect).
        `Stop` and `SessionEnd` on the hook pipe reach here as the state's
        `stop_event`; the relay sends `finalize` and the client answers with its drain
        and `eof` — per turn for a `Stop`, for good at `SessionEnd`."""
        # The thread before the tail, filed under the project its cwd names —
        # `ClaudeHookIngest.start_session`'s ordering, because `attach_remote` falls
        # back to a project-less create and a thread's project is frozen at creation.
        # Only a session on this same machine gets one: a project names server-local
        # directories, and a remote cwd naming one of them would be a false match.
        # TODO: assign remote sessions their project once projects can span machines.
        client = websocket.client
        if client is not None and client.host in {"127.0.0.1", "::1"}:
            holder = self.projects.resolve(Path(hello.cwd)) if hello.cwd else None
            project = self.projects.get(holder) if holder is not None else None
        else:
            project = None
        await self.threads.ensure(
            ThreadKey(self.native_id, "thread", hello.session_id),
            project=project,
        )
        state, offsets = await self.session_tailer.attach_remote(
            hello.session_id, Path(hello.transcript_path), sender
        )
        conversation = state.conversation
        if conversation is None:
            raise RuntimeError(f"session {hello.session_id} attached without a home")
        # How much of the session file is kept, so the session can be forked onto
        # another surface; only its owner's bytes, and only a registered owner's.
        # Read before the welcome, so a client leaving at once cancels no read.
        kept = (
            (
                await self.files.get(
                    conversation.transcript_file_id,
                    owner_id=sender.user_id,
                )
            ).size
            if conversation.transcript_file_id is not None
            and sender.user_id is not None
            else 0
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
                # `finalize`'s bounded wait covers the silence.
                logger.debug(
                    "session %s: finalize relay lost its socket", hello.session_id
                )

        relay = asyncio.create_task(relay_finalize())
        # Per-file contiguity: each line must start where the last one ended, so a
        # dropped frame surfaces as a close (4000 — the client reconnects and re-asks)
        # instead of a silently mis-assembled turn. The welcome's map, already sent,
        # doubles as the tracker.
        expected_offsets = offsets
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
                if message.agent_id is None and sender.user_id is not None:
                    kept = await self.keep_transcript(
                        conversation, message, kept, owner_id=sender.user_id
                    )
        except WebSocketDisconnect:
            pass
        except ValidationError:
            await websocket.close(code=1008, reason="unparseable stream message")
        except Exception:
            logger.exception(
                "session %s: remote tail errored; its remaining turns are left for "
                "the next connect to recover",
                hello.session_id,
            )
        finally:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay
            if clean:
                await self.session_tailer.finish_remote(state)
                with contextlib.suppress(Exception):
                    await websocket.close()
            else:
                self.session_tailer.detach_remote(state)

    async def keep_transcript(
        self,
        conversation: Conversation,
        line: StreamLine,
        kept: int,
        *,
        owner_id: UUID7,
    ) -> int:
        """Append one session-file line to the native conversation's stored
        transcript, and answer how far the stored bytes now reach.

        A line already kept is skipped, as a reconnect resends the turn it was in.
        One that does not start where the stored bytes end is not kept at all: the
        file has to be the session's own bytes, or a fork of it would replay
        something Claude never wrote.
        """
        data = line.line.encode() + b"\n"
        if line.start != kept or len(data) != line.end - line.start:
            return kept
        await self.conversations.store_transcript(
            UploadFile(
                BytesIO(data),
                filename=f"{conversation.external_id or conversation.id}.jsonl",
                headers=Headers({"content-type": "application/jsonl"}),
            ),
            line.start,
            conversation=conversation,
            files=self.files,
            owner_id=owner_id,
        )
        return line.end

    async def read_fork_transcript(
        self, source: Conversation, *, owner_id: UUID7
    ) -> tuple[bytes, ExternalAgentRun]:
        """The stored history up to the latest turn kept whole, and that turn."""
        if source.agent_tentacle_id != self.native_id or source.subagent_id:
            raise ValueError("Only root native Claude Code sessions can be forked here")
        if source.transcript_file_id is None or source.external_id is None:
            raise ValueError("Native Claude Code history has not been uploaded")
        data = await self.files.read(source.transcript_file_id, owner_id=owner_id)
        completed = max(
            (
                run
                for run in source.runs
                if isinstance(run, ExternalAgentRun)
                and run.native_session_id == source.external_id
                and run.end_offset is not None
                and run.end_offset <= len(data)
            ),
            key=lambda run: run.end_offset or 0,
            default=None,
        )
        if completed is None or completed.end_offset is None:
            raise ValueError("Native Claude Code history has no completed turn yet")
        return data[: completed.end_offset], completed

    async def fork_transcript(
        self,
        source: Conversation,
        target: Conversation,
        *,
        owner_id: UUID7,
        cwd: Path,
    ) -> Conversation:
        """Lay the kept history where `cwd` files Claude's sessions and fork it there
        with fresh message ids, for this tentacle to resume; the staged copy goes."""
        conversations = self.conversations
        async with conversations.lock(target.key):
            target = await conversations.get(target.id)
            if target.messages or target.external_id:
                raise ValueError("Transcript imports require an empty target")
            if source.external_id is None:
                raise ValueError("Native Claude Code history has not been uploaded")
            data, completed = await self.read_fork_transcript(source, owner_id=owner_id)
            staged = transcripts_dir(cwd) / f"{source.external_id}.jsonl"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            try:
                external_id = await self.ink.fork_session(
                    source.external_id, cwd=cwd, source_cwd=cwd
                )
            finally:
                staged.unlink()
            await conversations.fork(
                source,
                target,
                external_id=external_id,
                end_offset=completed.end_offset,
                permission_mode=completed.permission_mode,
            )
            return await conversations.get(target.id)

    async def fork_session(self, conversation: Conversation, *, cwd: Path) -> str:
        """Fork Claude's transcript without changing the source session."""
        if not conversation.external_id:
            raise ValueError("Cannot fork a Claude conversation without a session id")
        return await self.ink.fork_session(conversation.external_id, cwd=cwd)

    async def relocate(self, conversation: Conversation, *, cwd: Path) -> None:
        """Claude files a session under the cwd it ran in and resumes it only from
        there, so the transcript goes where the next run will look for it. A
        conversation with no session yet has nothing to move."""
        if conversation.external_id is None:
            return
        relocate_session(conversation.external_id, cwd=cwd)

    async def _await_human(
        self,
        requests: DeferredToolRequests,
        *,
        suspender: DeferredSuspender | None,
        interjections: Interjections[Message],
    ) -> tuple[DeferredActionBatch, DeferredActionBatchResponse | None]:
        """Pause the run on a human through the graph's suspender, putting the
        batch on this run's stream when that is what presents it. The reply waiter
        is registered before the cards go up, so a quick answer arriving through
        `Octomate.kick` cannot miss it. The Claude session stays open in-process
        while this awaits, so the answer is not durable across an Octomate restart.

        Returns `(batch, None)` if the wait exceeds `config.approval_timeout`; the
        batch is marked expired and the caller denies the pending tool so the live
        run unblocks."""
        if suspender is None:
            raise RuntimeError(
                "a Claude approval mid-run needs a suspender to pause on"
            )
        batch_id: UUID7 = uuid7()
        future: asyncio.Future[DeferredActionBatchResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pendings[batch_id] = future
        try:
            batch, event = await suspender.pause(requests, batch_id=batch_id)
            if event is not None:
                interjections.interject(event)
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

    async def probe_commands(self, context: CommandContext) -> CommandCatalog:
        """Initialize a non-persisting SDK client in the resolved workspace.

        The SDK caches initialize metadata, so an explicit refresh needs a new
        client. Match driven settings and resume identity without sending a query
        or preparing the workspace. Octomate's SDK MCP server exposes tools only;
        there are no MCP prompts to add to this catalog.
        """
        catalog = CommandCatalog(
            context=context, status="ready", descriptors=set(CONTROL_COMMANDS.values())
        )
        if context.cwd is None or not await anyio.Path(context.cwd).is_dir():
            catalog.limitations.append(
                "Claude native command discovery requires an existing workspace."
            )
            return catalog
        conversation = context.conversation
        project = (
            await self.run_project(conversation.thread_id)
            if conversation is not None
            else None
        )
        external_id = conversation.external_id if conversation is not None else None
        session_id = external_id or str(uuid7())
        async with self.driving(session_id):
            info = await self.ink.inspect(
                ClaudeAgentOptions(
                    cwd=str(context.cwd),
                    add_dirs=[str(root) for root in project.extra_roots]
                    if project
                    else [],
                    model=context.model,
                    permission_mode=context.permission_mode
                    if is_claude_mode(context.permission_mode)
                    else self.config.permission_mode,
                    resume=external_id,
                    session_id=None if external_id else session_id,
                    extra_args={"safe-mode": None, "no-session-persistence": None},
                    strict_mcp_config=True,
                )
            )
        if info.commands is None:
            catalog.limitations.append(
                "This Claude runtime does not expose native command metadata."
            )
            return catalog
        catalog.descriptors.update(
            entry.model_copy(
                update={
                    "unavailable_reason": HOST_COMMANDS[entry.id],
                    "unavailable_kind": "unsupported",
                }
            )
            if entry.id in HOST_COMMANDS
            else entry
            for entry in info.commands
            if entry.id not in CONTROL_COMMANDS
        )
        catalog.limitations.append(
            "Claude safe mode disables local commands, skills and plugins."
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
        """Dispatch a native command, retaining direct output outside model history."""
        conversation = context.conversation
        if conversation is None:
            raise ValueError("command execution requires a conversation")
        catalog = await self.discover_commands(context)
        descriptors = {
            entry.id: entry
            for entry in catalog.descriptors
            if isinstance(entry, ClaudeCommandDescriptor)
        }
        descriptor = descriptors.get(invocation.command_id)
        if descriptor is None:
            yield CommandError(
                status="stale", message="This command changed; refresh commands."
            )
            return
        if descriptor.id in CONTROL_COMMANDS:
            yield await self.command_control(context, invocation)
            return
        prompt = f"/{descriptor.name}"
        if invocation.arguments:
            prompt += f" {invocation.arguments}"
        events = self._iter_events(
            prompt,
            command=descriptor,
            conversation_address=context.address,
            thread_id=conversation.thread_id,
            conversation_id=conversation.id,
            run_name=descriptor.name,
            model=context.model,
            deferred_suspender=deferred_suspender,
            capabilities=capabilities,
        )
        try:
            async with contextlib.aclosing(events):
                async for event in events:
                    yield event
        except LookupError as error:
            yield CommandError(status="stale", message=str(error))

    async def command_control(
        self, context: CommandContext, invocation: CommandInvocation
    ) -> CommandOutcome:
        """Apply persistent planning permissions or fork without submitting a turn."""
        conversation = context.conversation
        assert conversation is not None
        argument = invocation.arguments.strip()
        if invocation.command_id == "plan":
            if argument not in {"", "on", "off"}:
                return CommandError(status="unsupported", message="Use /plan [on|off].")
            mode = "default" if argument == "off" else "plan"
            await self.set_permission_mode(conversation, mode)
            return CommandResult(
                segments=[TextSegment(data={"text": f"Permission mode: {mode}."})]
            )
        if argument:
            return CommandError(
                status="unsupported", message="/fork takes no arguments."
            )
        if conversation.external_id is None:
            return CommandError(
                status="unavailable", message="Run this conversation before forking it."
            )
        profile = await self.users.profile(
            context.address.channel_tentacle_id, context.address.user_id
        )
        channel = self.octomate.tentacles[context.address.channel_tentacle_id]
        if profile is None or profile.user_id != context.user_id:
            raise ValueError("The source conversation is no longer available.")
        if not isinstance(channel, ChannelTentacle):
            raise ValueError("Forking requires a channel that can open a thread.")
        source_thread = await self.threads.get(
            conversation.thread_id, with_messages=False
        )
        if source_thread is None:
            raise FileNotFoundError("No conversation")
        parent = replace(context.address, channel_thread_id=None)
        if parent.chat_type == "thread" and not isinstance(
            channel.config, TrunklineChannelConfig
        ):
            parent = replace(parent, chat_type="group")
        destination = await channel.start_thread(
            parent, self.threads.fork_title(conversation, source_thread) or "Fork"
        )
        target = await self.fork(
            conversation,
            ThreadKey.from_address(destination),
            sender=profile,
            model=context.model,
        )
        return CommandResult(
            segments=[
                TextSegment(
                    data={
                        "text": f"Forked into a new thread ({target.id}). Select it to continue; this conversation is unchanged."
                    }
                )
            ]
        )

    async def discover_models(self) -> None:
        session_id = str(uuid7())
        async with self.driving(session_id):
            info = await self.ink.inspect(
                ClaudeAgentOptions(
                    session_id=session_id,
                    extra_args={"safe-mode": None},
                    strict_mcp_config=True,
                )
            )
        provider = info.account.api_provider
        if provider is None or provider == "firstParty":
            provider = "anthropic"
        models: dict[str, Model | str] = {}
        claims: dict[str, Claim] = {}
        aliases: dict[str, str] = {}
        for model in info.models:
            # Named by the model it runs, so a pinned conversation keeps that model
            # when Claude Code's own names move on; the first entry describes it.
            resolved = model.resolved_model or model.value
            key = f"{provider}:{resolved}"
            aliases[model.value] = key
            if key in models:
                continue
            configured = self.config.claims.get(key)
            if model.supported_effort_levels is not None:
                efforts: tuple[str, ...] = tuple(model.supported_effort_levels)
            elif model.supports_effort is False:
                efforts = ()
            else:
                efforts = configured.efforts if configured else ()
            models[key] = resolved
            claims[key] = Claim(
                model.description
                or (configured.ability if configured else model.display_name),
                efforts,
            )
        self.set_model_catalog(models, claims)
        self.model_aliases = aliases
        self.default_model = aliases.get("default")

    def served_model(self, name: str) -> str | None:
        """An entry by its own name, by a name Claude Code itself uses (`opus`,
        `anthropic:opus[1m]`), or by the model id a transcript records, which
        drops the context-window suffix an entry may carry: the larger window
        takes whatever history there is."""
        if name in self.models:
            return name
        bare = name.rpartition(":")[2]
        alias = self.model_aliases.get(bare) or self.model_aliases.get(
            bare.partition("[")[0]
        )
        if alias is not None:
            return alias
        windows = [
            key
            for key, model in self.models.items()
            if str(model).partition("[")[0] == bare
        ]
        return max(windows, key=lambda key: "[" in key, default=None)

    async def __aenter__(self) -> ClaudeCodeTentacle:
        self.commands.invalidate(agent_id=self.id)
        await self.discover_models()
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        """Cancel any approvals/questions still awaiting a human so their parked
        runs unblock instead of hanging shutdown. The pending tools are denied as
        the cancellation unwinds; the live sessions are not durable across this."""
        self.commands.invalidate(agent_id=self.id)
        await super().__aexit__(exc_type, exc_value, traceback)
        for future in list(self.pendings.values()):
            if not future.done():
                future.cancel()
        self.pendings.clear()
        await self.ink.close()
        # Cancel any live transcript follow loops.
        await self.session_tailer.shutdown()

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
        command: ClaudeCommandDescriptor | None = None,
    ) -> AsyncGenerator[CommandOutcome | ReactStreamEvent[str], None]:
        cli_model = model.model_name if isinstance(model, Model) else model
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
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
        effort = await self.resolve_effort(conversation, model=cli_model, effort=effort)
        if deferred_tool_results is not None:
            # A resumed run. The CLI takes no tool result back, so the graph's
            # resolution of the deferral is this turn's prompt, ledgered as one.
            user_prompt = self.resumed_prompt(deferred_tool_results)
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(user_prompt)
        interjections = Interjections[Message]()
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

        async def can_use_tool(
            tool_name: str,
            input_data: JsonObject,
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            if tool_name in session_allowed:
                return PermissionResultAllow(updated_input=input_data)
            if not interactive:
                # A non-interactive run (a commissioned accomplice) has no
                # human to ask — decline at once instead of presenting a card.
                return PermissionResultDeny(
                    message=f"{tool_name} needs an approval and this run has no "
                    "user to ask. Proceed another way, or report what you could "
                    "not do."
                )
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
                requests, suspender=deferred_suspender, interjections=interjections
            )
            # `can_use_tool` fires per tool call, so we built the batch with a
            # single approval — this is that one action.
            action = next(iter(batch.approvals))
            approved = response is not None and bool(
                response.approvals.get(action.id, False)
            )
            if approved and response is not None and response.allow_session:
                session_allowed.add(tool_name)
                await self.conversations.grant_session_tool(conversation, tool_name)
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
            if not interactive:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "This run has no user to "
                        "ask. Proceed on your best judgment and state the "
                        "assumption in your report.",
                    }
                }
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
                                    multi_select=bool(item.get("multiSelect", False)),
                                )
                                for item in asked
                            ]
                        },
                        tool_call_id=tool_use_id or "AskUserQuestion",
                    )
                ]
            )
            batch, response = await self._await_human(
                requests, suspender=deferred_suspender, interjections=interjections
            )
            answered = (
                [
                    f"{question.args['question']}: "
                    + (answer if isinstance(answer, str) else ", ".join(answer))
                    for question in sorted(batch.questions)
                    if (answer := response.answers.get(question.id))
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

        # Resuming names the session; a new one is pinned before launching the CLI.
        session_id = conversation.external_id or str(uuid7())
        prompt_id: str | None = None

        async def remember_prompt(
            hook_input: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            nonlocal prompt_id
            event = ClaudeHookInput.model_validate(hook_input)
            if not event.prompt_id:
                raise ValueError("Claude's prompt hook did not provide a prompt_id")
            prompt_id = event.prompt_id
            return {}

        project = await self.run_project(conversation.thread_id)
        # Settled here, forked when the run enters it below: the options this builds
        # have to name the directory before the process that will run there exists.
        workspace = self.workspaces.open(conversation.thread_id, project)
        run_cwd = str(workspace.path)
        octomate_session = next(
            (
                capability.session
                for capability in capabilities or ()
                if isinstance(capability, GatewayCapability)
            ),
            None,
        )
        # The turn's server, mounted in process with the session closed over —
        # identity by closure, nothing on the wire names it. Its tools take the
        # normal tool-approval route like any other MCP tool; deliberately
        # nothing goes into `allowed_tools`.
        mcp_servers: dict[str, McpServerConfig] = {}
        if octomate_session is not None:
            mcp_servers[OCTOMATE_SERVER_NAME] = await octomate_mcp_server(
                octomate_session,
                self.threads,
                manager=self.mcp,
            )
        appended = "\n\n".join(
            part
            for part in (
                instructions if isinstance(instructions, str) else None,
                octomate_instructions() if octomate_session is not None else None,
            )
            if part
        )
        env = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
        if self.config.instrument:
            trace_environment = octomate_trace_environment()
            if trace_environment is not None:
                env.update(trace_environment.as_env())
                env.update(
                    CLAUDE_CODE_ENABLE_TELEMETRY="1",
                    CLAUDE_CODE_ENHANCED_TELEMETRY_BETA="1",
                )
        options = ClaudeAgentOptions(
            cwd=run_cwd,
            # A project's other roots are directories this work legitimately spans —
            # a settings tree, a sibling checkout — so Claude may reach them too.
            add_dirs=[str(root) for root in project.extra_roots] if project else [],
            model=cli_model,
            effort=claude_effort_adapter.validate_python(effort)
            if effort is not None
            else None,
            # Stored in the SDK's own vocabulary, so it goes over untranslated.
            permission_mode=(
                conversation.permission_mode
                if is_claude_mode(conversation.permission_mode)
                else self.config.permission_mode
            ),
            max_turns=self.config.max_turns,
            resume=conversation.external_id,
            session_id=None if conversation.external_id else session_id,
            can_use_tool=can_use_tool,
            hooks={
                "UserPromptSubmit": [HookMatcher(hooks=[remember_prompt])],
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[ask_user_question])
                ],
            },
            mcp_servers=mcp_servers,
            extra_args={"safe-mode": None, "thinking-display": "summarized"},
            strict_mcp_config=True,
            output_format=output_format,
            # Stream partial assistant messages so the accumulator can emit token
            # deltas (typewriter) instead of whole blocks; see ClaudeRunAccumulator.
            include_partial_messages=True,
            # Run-level instructions are real instructions, not prompt text:
            # appended to the Claude Code system-prompt preset so the SDK weighs
            # them as such (an accomplice's framing included), the gateway's
            # routing contract riding along when the turn mounts it.
            system_prompt=(
                SystemPromptPreset(type="preset", preset="claude_code", append=appended)
                if appended
                else None
            ),
            # Native Claude clients hide sdk-py transcripts from history. Tag these
            # user-routed sessions like CLI runs so they stay visible there too.
            env=env,
            # The CLI's stderr is the only place it says why it exited: the SDK's
            # `ProcessError` carries the exit code and "check stderr", nothing else.
            stderr=lambda line: logger.warning(
                "session %s: claude stderr: %s", session_id, line.rstrip()
            ),
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

        with claude_logfire.span(
            "ClaudeCodeTentacle {agent_id} {run_name} [{conversation_address}]",
            agent_id=self.id,
            run_name=run_name or "claude",
            conversation_address=str(conversation_address),
            **agent_input_message_attributes(user_prompt),
            transport="local",
        ):
            # Entered first so it leaves last: the tree exists before the CLI is
            # launched into it, and a chat thread's is only thrown away once the CLI
            # holding it open has been waited out.
            # The SDK injects the active W3C context when connecting. Starting a
            # client inside each run's span also reparents resumed sessions.
            async with (
                workspace,
                self.driving(session_id),
                contextlib.aclosing(
                    self.ink.stream(
                        prompt_text,
                        options=options,
                        conversation_id=conversation.id,
                        should_interrupt=lambda: (
                            octomate_session is not None
                            and isinstance(octomate_session.decision, TeleportDecision)
                        ),
                        command=command,
                    )
                ) as native_messages,
                contextlib.aclosing(interjections.around(native_messages)) as messages,
            ):
                self.commands.invalidate(
                    agent_id=self.id, conversation_id=conversation.id
                )
                command_result: ResultMessage | None = None
                async for message in messages:
                    if isinstance(message, ActionBatchEvent):
                        yield message
                        continue
                    if command is not None and isinstance(message, ResultMessage):
                        command_result = message
                    for event in accumulator.consume(
                        message, command=command is not None
                    ):
                        yield event
            if command is not None and command_result is None:
                raise RuntimeError("Claude ended without a command result.")
            run_id = str(uuid7())
            recorded_run = None
            if command is not None and accumulator.usage.requests == 0:
                if accumulator.session_id:
                    await self.conversations.set_external_id(
                        conversation, accumulator.session_id
                    )
            else:
                recorded_run = await self.conversations.record_agent_run(
                    conversation,
                    run_id=run_id,
                    messages=accumulator.messages,
                    name=run_name,
                    model_name=accumulator.model_name or cli_model,
                    permission_mode=options.permission_mode,
                    cwd=Path(run_cwd),
                    external_id=accumulator.session_id,
                    native_id=CLAUDE_NATIVE_ID,
                    native_turn_id=prompt_id,
                )
            if command_result is not None and command_result.is_error:
                raise RuntimeError(
                    command_result.result
                    or "; ".join(command_result.errors or ())
                    or f"Claude command failed: {command_result.subtype}"
                )
            if command is not None and recorded_run is None:
                yield CommandResult(
                    segments=[TextSegment(data={"text": accumulator.result_text})]
                    if accumulator.result_text
                    else []
                )
                return
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
            moving = octomate_session.decision if octomate_session is not None else None
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
                            requests,
                            run_id=run_id,
                            conversation_id=str(conversation.id),
                        ),
                    )
                )
                return
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
            deferred_tool_results=deferred_tool_results,
            deferred_suspender=deferred_suspender,
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
        source = self._iter_events(
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

        async def events() -> AsyncGenerator[ReactStreamEvent[str], None]:
            async with contextlib.aclosing(source):
                async for event in source:
                    if isinstance(event, CommandResult | CommandError):
                        raise RuntimeError(
                            "A Claude agent run yielded a command outcome"
                        )
                    yield event

        return ReactEventStream(events())
