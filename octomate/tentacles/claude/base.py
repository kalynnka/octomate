from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import weakref
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    ClassVar,
    cast,
    get_args,
    overload,
)

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    PreToolUseHookInput,
    ToolPermissionContext,
)
from claude_agent_sdk.types import SystemPromptPreset
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
from pydantic import TypeAdapter, ValidationError
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
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import AbstractToolset
from rich.style import Style
from uuid_utils.compat import uuid7

from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.config.agents import ClaudeCodeConfig
from octomate.mcp.server import OCTOMATE_SERVER_NAME, octomate_instructions
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.base import sqlalchemy_materia
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
from octomate.schemas.thread import CLAUDE_NATIVE_ID, ThreadKey
from octomate.schemas.triage import TeleportDecision
from octomate.schemas.user import UserProfile
from octomate.telemetry import claude_logfire
from octomate.tentacles.agent import AgentSpecInput, AgentTentacle
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.hooks import ClaudeHookInput
from octomate.tentacles.claude.ingest import ClaudeHookIngest
from octomate.tentacles.claude.mcp import octomate_mcp_server
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.claude.transcript import relocate_session
from octomate.tentacles.hooks import hook_guard, hook_sender
from octomate.tentacles.locks import SessionLocks
from octomate.types.json import JsonObject
from octomate.types.permissions import ClaudePermissionMode, is_claude_mode

if TYPE_CHECKING:
    from octomate.base import Octomate

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
    the gateway as the turn's in-process MCP server. A `teleport` cast through
    it interrupts the turn, which ends as the deferral the graph performs and
    resumes the agent from — in a sub-thread, a crossing, or a project's
    workspace — and a resumed run opens from what the graph resolved it with.
    """

    config: ClaudeCodeConfig = field(init=False)

    # A Claude run stays live in-process; `pending` (from `AgentTentacle`) parks a
    # waiter per gated tool / question until `Octomate.kick` delivers the response.
    in_process: ClassVar[bool] = True

    permission_modes: ClassVar[tuple[str, ...]] = get_args(ClaudePermissionMode)

    @property
    def default_permission_mode(self) -> str | None:
        return self.config.permission_mode

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
        description: str | None = None,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.description = description or self.description
        self.pending = {}
        # Not `dict(...)`, which C416 asks for: each config's claims are keyed by that
        # runtime's own narrower literal, and `Mapping`'s key is invariant — only the
        # comprehension widens them to `AgentRouteModelName` without a cast.
        self.claims = {  # noqa: C416
            model: claim for model, claim in config.claims.items()
        }
        self.gateway = config.gateway
        # One live Claude client per conversation, keyed by conversation id: a new
        # turn interrupts the prior run for the same conversation (Phase 6). Not
        # thread id — a thread also holds subagent conversations, whose runs must
        # neither interrupt the thread's own live run nor each other. Weak values
        # so a finished run's client drops out on its own once it is collected.
        self.live_clients: weakref.WeakValueDictionary[uuid.UUID, ClaudeSDKClient] = (
            weakref.WeakValueDictionary()
        )
        self.models = {model: model for model in config.models}
        # Per-session locks shared by the hook ingest and the transcript tailer, so a
        # session's ledger writes (hooks) and run commits (tailer) serialize.
        self.session_locks = SessionLocks()
        # Assembles native sessions' turns from streamed transcript lines — the
        # stream is the only assembler; the server never opens a transcript.
        self.session_tailer = ClaudeTranscriptTailer(
            self.octomate.conversations,
            self.octomate.thread_manager,
            self.session_locks,
        )
        # Live hook ingest: writes the human ledger and relays the stream's drains.
        # Reads the same managers this tentacle writes, through the bound `octomate`.
        self.session_ingest = ClaudeHookIngest(
            self.octomate,
            self.session_tailer,
            self.session_locks,
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
        verifier = hook_guard(self.octomate.bearers, self.id)
        resolve_sender = hook_sender(self.octomate.users, CLAUDE_NATIVE_ID, verifier)
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
        """One remote tail's connection, up to its attach: take the hello and refuse
        what cannot stream — a stale protocol loudly (the session still degrades to
        hooks-only ingest), and a session this tentacle is driving itself, whose
        transcript ingested here would write the conversation a second time
        (`ClaudeHookIngest.driving`). Authentication already happened at the
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
            # A live claim only — the counter is in-memory, so a session resumed
            # natively after a restart is not caught; its ingest then lands in its
            # own native thread rather than colliding with the driven runs. A
            # durable guard is still to come.
            await websocket.close(code=1008, reason="octomate drives this session")
            return
        # Its own materia context: a stream outlives any request, like a follow loop.
        with sqlalchemy_materia():
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
            holder = (
                self.octomate.projects.resolve(Path(hello.cwd)) if hello.cwd else None
            )
            project = self.octomate.projects.get(holder) if holder is not None else None
        else:
            project = None
        await self.octomate.thread_manager.ensure(
            ThreadKey(CLAUDE_NATIVE_ID, "thread", hello.session_id),
            project=project,
        )
        state, offsets = await self.session_tailer.attach_remote(
            hello.session_id, Path(hello.transcript_path), sender
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

    async def relocate(self, conversation: Conversation, *, cwd: Path) -> None:
        """Claude files a session under the cwd it ran in and resumes it only from
        there, so the transcript goes where the next run will look for it. A
        conversation with no session yet has nothing to move."""
        if conversation.external_id is None:
            return
        relocate_session(conversation.external_id, cwd=cwd)

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
            target_mode="sub" if conversation_address.channel_thread_id else "main",
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
        # Cancel any live transcript follow loops.
        await self.session_tailer.shutdown()

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
        deferred_tool_results: DeferredToolResults | None = None,
        deferred_suspender: DeferredSuspender | None = None,
    ) -> AsyncGenerator[ReactStreamEvent[str], None]:
        if thread_id is None:
            raise ValueError("agent run requires a thread_id to own its conversation")
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
        if deferred_tool_results is not None:
            # A resumed run. The CLI takes no tool result back, so the graph's
            # resolution of the deferral is this turn's prompt, ledgered as one.
            user_prompt = self.resumed_prompt(deferred_tool_results)
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

        # Settle the session id before the CLI exists, so the hook pipe can be told to
        # leave this session alone (claimed below) before it can fire anything. Resuming
        # already names the session; a new one is pinned here — the SDK takes one or the
        # other, never both.
        session_id = conversation.external_id or str(uuid7())

        project = await self.run_project(conversation.thread_id)
        # Settled here, forked when the run enters it below: the options this builds
        # have to name the directory before the process that will run there exists.
        workspace = self.octomate.workspaces.open(conversation.thread_id, project)
        run_cwd = str(workspace.path)
        # `setting_sources` is left unset on purpose: verified against the CLI, the
        # unset default loads every source, so the bound directory arrives with its
        # own CLAUDE.md and .claude/settings.json — the useful half of "work on this
        # project". Setting it to ["project"] would be the same behavior spelled
        # loudly; setting it to [] would silently drop a repo's instructions.
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
        served = list(self.octomate.mcps.values())
        if octomate_session is not None:
            mcp_servers[OCTOMATE_SERVER_NAME] = await octomate_mcp_server(
                octomate_session, self.octomate.thread_manager, served
            )
        appended = "\n\n".join(
            part
            for part in (
                instructions if isinstance(instructions, str) else None,
                octomate_instructions(served) if octomate_session is not None else None,
            )
            if part
        )
        options = ClaudeAgentOptions(
            cwd=run_cwd,
            # A project's other roots are directories this work legitimately spans —
            # a settings tree, a sibling checkout — so Claude may reach them too.
            add_dirs=[str(root) for root in project.extra_roots] if project else [],
            model=cli_model,
            # The SDK scale has no `minimal` (and a `max` tier Octomate does not
            # offer); minimal maps down to low, the rest pass through. None
            # leaves the CLI default.
            effort="low" if effort == "minimal" else effort,
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
                "PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[ask_user_question])
                ]
            },
            mcp_servers=mcp_servers,
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
            env={"CLAUDE_CODE_ENTRYPOINT": "cli"},
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

        # Parked, not dropped: remote runs are off because nothing makes a workspace
        # on another machine. Restoring is this block, the import, the span label and
        # `transport=transport` on the client — never the config validator alone.
        # transport = (
        #     SSHTransport(prompt_text, options, ssh=self.config.ssh)
        #     if self.config.ssh is not None
        #     else None
        # )
        with (
            claude_logfire.span(
                "ClaudeCodeTentacle {agent_id} {run_name} [{conversation_address}]",
                agent_id=self.id,
                run_name=run_name or "claude",
                conversation_address=str(conversation_address),
                # transport=(
                #     f"ssh:{self.config.ssh.host}"
                #     if self.config.ssh is not None
                #     else "local"
                # ),
                transport="local",
            ),
            # Taken before the CLI is launched and held until its teardown has waited
            # the process out, so it spans every hook this session can fire.
            self.session_ingest.driving(session_id),
        ):
            # Entered first so it leaves last: the tree exists before the CLI is
            # launched into it, and a chat thread's is only thrown away once the CLI
            # holding it open has been waited out.
            # async with ClaudeSDKClient(options=options, transport=transport) as client:
            async with (
                workspace,
                ClaudeSDKClient(options=options) as client,
            ):
                # One live run per conversation: register this client and interrupt
                # any prior run for the same conversation so a mid-run follow-up
                # supersedes it. The weak-value map drops this entry once the run
                # ends and the client is collected — no manual deregistration.
                previous = self.live_clients.get(conversation.id)
                self.live_clients[conversation.id] = client
                if previous is not None and previous is not client:
                    with contextlib.suppress(Exception):
                        await previous.interrupt()
                await client.query(prompt_text)
                interrupted = False
                async for message in client.receive_response():
                    for event in accumulator.consume(message):
                        yield event
                    if (
                        not interrupted
                        and octomate_session is not None
                        and isinstance(octomate_session.decision, TeleportDecision)
                    ):
                        # Moving mid-run: the move is the graph's to perform, and
                        # this process is still where it was, so the turn ends now
                        # — as the deferral the graph performs and resumes from.
                        interrupted = True
                        await client.interrupt()
            run_id = str(uuid7())
            recorded_run = await self.octomate.conversations.record_agent_run(
                conversation,
                run_id=run_id,
                messages=accumulator.messages,
                name=run_name,
                cwd=Path(run_cwd),
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
                deferred_tool_results=deferred_tool_results,
                deferred_suspender=deferred_suspender,
            )
        )
