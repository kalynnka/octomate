"""The Trunkline web channel: the console drives Octomate through
`octomate.kick` and reads the run stream back natively.

Unlike the Vercel dev-UI channel it replaces, nothing is transcoded to a
third-party UI protocol: the SSE response carries the run's own event union
(the harness `WireEvent`), and the console renders those events directly.

`octomate.kick` pushes output through feelers, but the console needs an inline
SSE response. So `stream_kick` runs the kick in a background task with a
per-request `current_sink` active; the channel's timeline feeler forwards the
run stream into it, and the response encodes that to SSE. Keyed as a
`flat_thread` channel, every directive carries its thread id and streams
straight through without triage — a new id starts a new thread, an existing id
continues it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, TypeAlias

import anyio
from anyio import BrokenResourceError, ClosedResourceError
from anyio.streams.memory import MemoryObjectSendStream
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils import uuid7

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    RunErrorEvent,
    RunResultEvent,
    StreamEvents,
    SubagentActivity,
    SubagentActivityStatus,
    SubagentSettledEvent,
    SubagentStartedEvent,
    WireEvent,
    wire_event_adapter,
)
from octomate.config.channels import AgentModelConfig, TrunklineChannelConfig
from octomate.schemas.awakes import AwakeSignal, UserMessageSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.events import MessageEvent
from octomate.schemas.project import Project
from octomate.schemas.segments import ImageSegment, MessageSegment, TextSegment
from octomate.schemas.thread import Thread
from octomate.schemas.user import UserProfile
from octomate.tentacles.channels.base import (
    ChannelOutput,
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    IMMessageID,
    Ink,
    StaticMount,
    ThreadStrategy,
)
from octomate.tentacles.channels.feelers.deferred import ApprovalFeeler, QuestionFeeler
from octomate.tentacles.channels.feelers.output import (
    SubagentTimelineState,
    TimelineState,
)
from octomate.types.permissions import AgentPermissionMode

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

RunStreamItem: TypeAlias = (
    StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
)
TrunklineStreamItem: TypeAlias = (
    RunStreamItem | SubagentStartedEvent | SubagentSettledEvent
)

# The console is single-user until it grows authentication; the sender is fixed
# and every thread lives in the one private chat with them.
CONSOLE_USER_ID = "dev"

# Separator joining agent and model into the route id the console picker offers.
ROUTE_SEP = ":"

# Per-request output sink the kick's presenter streams into. The channel is a
# singleton across concurrent requests, so each request sets its own sink here.
current_sink: ContextVar[MemoryObjectSendStream[TrunklineStreamItem] | None]
current_sink = ContextVar("trunkline_output_sink", default=None)


async def send_quietly(
    sink: MemoryObjectSendStream[TrunklineStreamItem],
    item: TrunklineStreamItem,
) -> None:
    """Forward to the console while it is still listening. A departed console
    must not kill the run — it records to the thread ledger regardless; only
    the live mirror is gone."""
    try:
        await sink.send(item)
    except (BrokenResourceError, ClosedResourceError):
        pass


class TrunklineDirective(BaseModel):
    """One console turn: the directive text bound for a thread."""

    thread_id: str
    text: str
    message_id: str | None = None
    model: str | None = None
    project: str | None = None
    permission_mode: AgentPermissionMode | None = None


class RouteLockedError(Exception):
    """A directive tried to re-route a thread that already has an owner."""


class TrunklineSeamNotWired(NotImplementedError):
    """Raised by the platform send/encode seam, unused by the inline-SSE path."""

    def __init__(self) -> None:
        super().__init__(
            "The Trunkline channel streams inline over SSE; the platform "
            "send/encode seam is unused by the console delivery path."
        )


class TrunklineInk(Ink[WireEvent]):
    """Transport stub: only identity probing is used (output streams inline)."""

    async def inspect(self) -> UserProfile:
        return UserProfile(channel_user_id="trunkline", name="Octomate")

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(channel_user_id=user_id or CONSOLE_USER_ID, name="Console")

    async def upload_media(self, data: bytes) -> str | None:
        raise TrunklineSeamNotWired

    async def download_image(
        self, seg: ImageSegment, message_id: str
    ) -> DownloadedImage | None:
        raise TrunklineSeamNotWired

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[WireEvent],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        raise TrunklineSeamNotWired


class TrunklineChromo(Chromo[TrunklineDirective, WireEvent]):
    """Inbound translation only; outbound encoding is unused."""

    async def sip(self, raw: TrunklineDirective) -> MessageEvent | None:
        if not raw.text.strip():
            return None
        return MessageEvent(
            message_id=raw.message_id or uuid7().hex,
            channel_thread_id=raw.thread_id,
            user_id=CONSOLE_USER_ID,
            chat_id=CONSOLE_USER_ID,
            chat_type="thread" if raw.thread_id else "dm",
            segments=[TextSegment(data={"text": raw.text})],
            raw=raw.text,
        )

    def outbound_markdown(self, text: str) -> list[WireEvent]:
        raise TrunklineSeamNotWired


class TrunklineTimelineState(TimelineState):
    """Forwards the raw run stream to the request's sink (the console renders
    events itself), bypassing the default markdown-rotation lifecycle."""

    def __init__(
        self,
        address: ChannelAddress,
        ask_questions: QuestionFeeler,
        approvals: ApprovalFeeler,
        sink: MemoryObjectSendStream[TrunklineStreamItem],
    ) -> None:
        self.address = address
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.sink = sink
        self.message_id = None
        self.reply_to = None

    @asynccontextmanager
    async def open_subagent(
        self,
        activity: SubagentActivity,
    ) -> AsyncGenerator[TrunklineSubagentTimelineState, None]:
        state = TrunklineSubagentTimelineState(activity, self.sink)
        await state.start()
        yield state

    async def drive(
        self,
        stream: AsyncIterator[RunStreamItem],
    ) -> None:
        async for event in stream:
            await self.observe_subagent_event(event)
            await send_quietly(self.sink, event)


class TrunklineSubagentTimelineState(SubagentTimelineState):
    """Puts one commissioned child run's lifecycle on the wire."""

    def __init__(
        self,
        activity: SubagentActivity,
        sink: MemoryObjectSendStream[TrunklineStreamItem],
    ) -> None:
        self.activity = activity
        self.sink = sink
        self.response = ""
        self.settled = False

    async def start(self) -> None:
        await send_quietly(
            self.sink,
            SubagentStartedEvent(
                invocation_id=self.activity.invocation_id,
                kind=self.activity.kind,
                name=self.activity.name,
            ),
        )

    async def append_response(self, delta: str) -> None:
        if self.settled or not delta:
            return
        self.response += delta

    async def settle(
        self,
        status: SubagentActivityStatus,
        detail: str | None = None,
    ) -> None:
        if self.settled:
            return
        self.settled = True
        await send_quietly(
            self.sink,
            SubagentSettledEvent(
                invocation_id=self.activity.invocation_id,
                status=status,
                detail=detail,
                response=self.response,
            ),
        )


class TrunklineQuestionFeeler(QuestionFeeler):
    """Presents ask feelers as `action_batch` wire events on the active sink.

    With no active console request (a hook-ingested agent run, a departed
    client), the persisted batch still surfaces through the thread detail's
    `pending` list on the next load."""

    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[uuid.UUID, IMMessageID | None]:
        sink = current_sink.get()
        if sink is not None and actions:
            await send_quietly(
                sink,
                ActionBatchEvent(
                    batch_id=str(actions[0].batch_id),
                    questions=actions,
                ),
            )
        return {action.id: None for action in actions}


class TrunklineApprovalFeeler(ApprovalFeeler):
    """Approval twin of `TrunklineQuestionFeeler`."""

    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredApproval],
    ) -> dict[uuid.UUID, IMMessageID | None]:
        sink = current_sink.get()
        if sink is not None and actions:
            await send_quietly(
                sink,
                ActionBatchEvent(
                    batch_id=str(actions[0].batch_id),
                    approvals=actions,
                ),
            )
        return {action.id: None for action in actions}


class TrunklineTimelineFeeler:
    """Opens a per-run timeline that streams into the active request's sink."""

    def __init__(
        self, *, ask_questions: QuestionFeeler, approvals: ApprovalFeeler
    ) -> None:
        self.ask_questions = ask_questions
        self.approvals = approvals

    @asynccontextmanager
    async def open(
        self, address: ChannelAddress
    ) -> AsyncGenerator[TrunklineTimelineState, None]:
        sink = current_sink.get()
        if sink is None:
            raise RuntimeError(
                "trunkline timeline opened without an active request sink"
            )
        state = TrunklineTimelineState(
            address, self.ask_questions, self.approvals, sink
        )
        try:
            yield state
        except asyncio.CancelledError:
            await state.settle_subagents("cancelled")
            raise
        finally:
            await state.settle_subagents("failed")


def to_wire(item: TrunklineStreamItem) -> WireEvent | None:
    """One run-stream item as its wire event, or None for the shapes the wire
    replaces (see the wire module docstring)."""
    match item:
        case AgentRunResultEvent():
            match item.result.output:
                case str() | None:
                    output: str | list[MessageSegment] | None = item.result.output
                case DeferredToolRequests():
                    # The suspension already went out as an ActionBatchEvent.
                    output = None
                case segments:
                    output = list(segments)
            return RunResultEvent(output=output, usage=item.result.usage)
        case FinalResult():
            return None
        case _:
            return item


SSE_HEADERS = {
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class TrunklineTentacle(ChannelTentacle[TrunklineDirective, WireEvent]):
    """Channel that serves the Trunkline web console over `octomate.kick`."""

    # Routing only: the chromo always sets a thread_id, so a directive continues
    # its own thread without triage. There is no seam to open a sub-thread and no
    # DM surface, so `surfaces` stays empty.
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    # The built console SPA, resolved from the serving directory like
    # FILES_ROOT is. `static_mounts` skips it while unbuilt.
    CONSOLE_DIST: ClassVar[Path] = Path("trunkline/dist")

    config: TrunklineChannelConfig

    def __init__(
        self, id: str, octomate: Octomate, *, config: TrunklineChannelConfig
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=TrunklineInk(),
            chromo=TrunklineChromo(),
            config=config,
        )
        # Deferred actions reach the console as wire events, not markdown —
        # claude/codex `_await_human` presents through these directly.
        self.feelers.ask_questions = TrunklineQuestionFeeler()
        self.feelers.approvals = TrunklineApprovalFeeler()
        self.feelers.timeline = TrunklineTimelineFeeler(
            ask_questions=self.feelers.ask_questions,
            approvals=self.feelers.approvals,
        )
        # Kicks outlive their request on client disconnect; hold them so the
        # event loop keeps them alive to completion.
        self.background_kicks: set[asyncio.Task[None]] = set()

    def routers(self) -> tuple[APIRouter]:
        """The console HTTP surface, mounted by `Octomate.connect`."""
        # Local import: routes.py imports this module, so a module-level import cycles.
        from octomate.tentacles.channels.web.trunkline.routes import (
            build_trunkline_router,
        )

        return (build_trunkline_router(self.octomate, channel_id=self.id),)

    def static_mounts(self) -> tuple[StaticMount, ...]:
        """The console SPA at the app root — one entry serves UI and API.
        Empty when config opts out (Vite dev server, separate deployment)."""
        if not self.config.serve_console:
            logger.info(
                "Trunkline console serving disabled by config; the API serves "
                "under /api/trunkline."
            )
            return ()
        if not self.CONSOLE_DIST.is_dir():
            logger.info(
                "Trunkline console assets not built (%s missing); the API still "
                "serves under /api/trunkline — run `pnpm build` in trunkline/ or "
                "use the Vite dev server.",
                self.CONSOLE_DIST,
            )
            return ()
        logger.info("Trunkline console enabled — serving %s at /.", self.CONSOLE_DIST)
        return (
            StaticMount(
                path="/",
                directory=self.CONSOLE_DIST,
                name="trunkline-console",
            ),
        )

    def routable_agents(self) -> list[AgentModelConfig]:
        """Every agent-model route the console can open a thread on.

        Wider than this channel's `agents:` list, which is the entry routing a
        chat platform needs: nobody walks into the console, so the operator picking
        an agent should see every model every registered tentacle runs. The
        channel's own entries come first all the same, so the picker's default is
        still this channel's entry agent.
        """
        declared = [
            agent_config
            for agent_config in self.config.agents
            if agent_config.agent in self.octomate.agents
        ]
        seen = {(agent_config.agent, agent_config.model) for agent_config in declared}
        return declared + [
            AgentModelConfig(agent=agent_id, model=model)
            for agent_id, agent in self.octomate.agents.items()
            for model in agent.models
            if (agent_id, model) not in seen
        ]

    def chosen_project(self, name: str | None) -> Project | None:
        """The registered project a directive named, or None when it named none.

        The console is where a project is chosen, since a console thread has no
        directory of its own to be filed from — and no project is a real answer: the
        thread is then a chat, and its agent works nowhere in particular. An
        unregistered name is refused rather than dropped, because a typo would
        otherwise read as that answer.
        """
        if name is None:
            return None
        project = self.octomate.projects.get(name)
        if project is None or not project.enabled:
            raise ValueError(f"no enabled project is registered as {name!r}")
        return project

    async def claim_route(self, thread: Thread, selected_model: str | None) -> None:
        """The route is chosen at thread creation only: the first directive's
        pick records the entry handoff. Once a thread has an owner the route is
        fixed — a mid-thread model switch busts the provider's KV cache — so a
        differing pick is refused until an explicit manual handoff exists."""
        if thread.active_agent_tentacle_id is not None:
            if selected_model is None:
                return
            active = (
                f"{thread.active_agent_tentacle_id}{ROUTE_SEP}{thread.active_model}"
            )
            if selected_model == active:
                return
            raise RouteLockedError(
                f"thread is routed to {active!r}; the route is fixed after the "
                "first directive — switching needs a manual handoff"
            )
        if selected_model is None:
            return
        chosen = next(
            (
                agent_config
                for agent_config in self.routable_agents()
                if f"{agent_config.agent}{ROUTE_SEP}{agent_config.model}"
                == selected_model
            ),
            None,
        )
        if chosen is None:
            return
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=chosen.agent
        )
        await self.octomate.thread_manager.record_handoff(
            thread,
            from_agent_tentacle_id=None,
            to_agent_tentacle_id=chosen.agent,
            to_model=chosen.model,
            reason="console route selection",
            target_conversation_id=conversation.id,
        )

    async def claim_posture(self, thread: Thread, mode: AgentPermissionMode) -> None:
        """Store the posture a directive picked on the conversation about to run it.

        This is the console picking one before there is anything to pick it on: a
        thread being composed has no row yet, and by the time it does the run that
        would have honored the pick is already going. An owned thread's posture is
        switched directly on its conversation instead.

        Which agent it belongs to is the thread's own, claimed just above. A posture
        with no agent to read it is refused rather than stored against a guess — the
        vocabularies do not overlap, so guessing would be picking a provider.
        """
        agent_tentacle_id = thread.active_agent_tentacle_id
        if agent_tentacle_id is None:
            raise ValueError(
                "a permission mode is one agent's vocabulary and this thread is "
                "routed to none; pick a route on the directive that creates it"
            )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=agent_tentacle_id
        )
        await self.octomate.conversations.set_permission_mode(conversation, mode)

    def stream_kick(self, signal: AwakeSignal) -> StreamingResponse:
        """Run the kick in a free task with this request's sink active and
        encode the run stream it forwards as an SSE response.

        The task is deliberately not tied to the response: a client that
        disconnects mid-run just stops watching — the run finishes and records
        to the thread ledger regardless (`send_quietly` turns further sink
        sends into no-ops once the receive side is gone). Nothing here opens a
        cancel scope inside the generator, so a disconnect-time GC finalize
        cannot corrupt one from another task.
        """
        send, receive = anyio.create_memory_object_stream[TrunklineStreamItem](128)
        captured: list[Exception] = []

        async def pump() -> None:
            token = current_sink.set(send)
            try:
                await self.octomate.kick(signal)
            except Exception as exc:
                logger.error("Trunkline kick failed", exc_info=exc)
                captured.append(exc)
            finally:
                current_sink.reset(token)
                await send.aclose()

        task = asyncio.create_task(pump())
        self.background_kicks.add(task)
        task.add_done_callback(self.background_kicks.discard)

        def frame(event: WireEvent) -> str:
            payload = wire_event_adapter.dump_json(event, warnings=False)
            return f"data: {payload.decode()}\n\n"

        async def events() -> AsyncIterator[str]:
            async with receive:
                async for item in receive:
                    wire_event = to_wire(item)
                    if wire_event is not None:
                        yield frame(wire_event)
                for error in captured:
                    yield frame(RunErrorEvent(message=str(error)))

        return StreamingResponse(
            events(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    async def handle_directive(
        self, directive: TrunklineDirective
    ) -> StreamingResponse:
        event = await self.chromo.sip(directive)
        if event is None:
            raise ValueError("directive carried no text")
        event.tentacle_id = self.id
        event.self_id = self.self_profile.channel_user_id
        event.sender = await self.get_user_profile(event.user_id)
        thread = await self.octomate.thread_manager.ensure(
            ChannelAddress(
                channel_tentacle_id=self.id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                user_id=event.user_id,
                channel_thread_id=event.channel_thread_id,
                shared=event.shared,
            ),
            project=self.chosen_project(directive.project),
        )
        await self.claim_route(thread, directive.model)
        if directive.permission_mode is not None:
            await self.claim_posture(thread, directive.permission_mode)
        thread_message = await self.octomate.thread_manager.record_inbound(event)
        return self.stream_kick(
            UserMessageSignal([event], trigger_thread_message_id=thread_message.id)
        )
