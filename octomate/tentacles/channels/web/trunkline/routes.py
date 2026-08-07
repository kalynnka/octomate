"""The Trunkline console HTTP surface, mounted under `/api/trunkline`.

The console is an entry and a reader: `GET /threads` lists every channel's
threads and `GET /threads/{id}` reads any of them, while directives create or
continue threads on the trunkline channel itself. The two POST endpoints
stream: a directive streams its run, and a batch response streams the resumed
run, both as SSE of the native wire events (see `wire`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import AfterValidator, BaseModel, Field

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.config.agents import AgentRouteModelName
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.messages import native_utc
from octomate.schemas.thread import (
    ChannelActorKind,
    Thread,
    ThreadMessage,
    ThreadStatus,
)
from octomate.tentacles.channels.web.trunkline.base import (
    CONSOLE_USER_ID,
    ROUTE_SEP,
    RouteLockedError,
    TrunklineDirective,
    TrunklineTentacle,
)

if TYPE_CHECKING:
    from octomate import Octomate

# SQLite strips tzinfo; re-attach UTC so the browser doesn't parse these as
# local time (the message ledger already does this at the schema).
UtcDateTime = Annotated[datetime, AfterValidator(native_utc)]


class RouteInfo(BaseModel):
    id: str = Field(description="The route id a directive's `model` field names.")
    agent: str
    model: AgentRouteModelName


class ThreadSummaryInfo(BaseModel):
    id: uuid.UUID = Field(
        description="The thread row id — the read key for GET /threads/{id}."
    )
    channel: str = Field(description="Owning channel tentacle id.")
    thread_key: str = Field(
        description="The platform thread id; for trunkline threads, the "
        "directive key. Empty for a channel's main-chat thread."
    )
    title: str
    status: ThreadStatus
    agent: str | None
    model: AgentRouteModelName | None
    updated_at: UtcDateTime
    message_count: int


class LedgerEntry(BaseModel):
    id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    actor_kind: ChannelActorKind
    agent: str | None
    sender: str
    happened_at: UtcDateTime
    text: str


class SessionEntry(BaseModel):
    """One handoff: the point an agent-model route took the thread over."""

    id: uuid.UUID
    from_agent: str | None
    to_agent: str
    model: AgentRouteModelName | None
    reason: str
    created_at: UtcDateTime


class ThreadDetailInfo(ThreadSummaryInfo):
    entries: list[LedgerEntry]
    sessions: list[SessionEntry]
    pending: list[ActionBatchEvent] = Field(
        description="Unanswered action batches, in the same shape the stream's "
        "`action_batch` events use, so the console re-renders waiting feelers "
        "on reload."
    )


class DirectiveBody(BaseModel):
    text: str
    message_id: str | None = None
    model: str | None = Field(
        default=None,
        description="A route id from GET /routes ('agent:model'). Honored only "
        "on a thread's first directive; a differing pick on an owned thread is "
        "refused with 409 (re-routing needs a manual handoff).",
    )


class BatchResponseBody(BaseModel):
    answers: dict[uuid.UUID, str] = Field(default_factory=dict)
    approvals: dict[uuid.UUID, bool] = Field(default_factory=dict)
    allow_session: bool = False


def thread_title(thread: Thread) -> str:
    for message in thread.messages:
        if message.actor_kind == "human" and message.message_text:
            return message.message_text.splitlines()[0][:80]
    return "(untitled)"


def thread_summary(thread: Thread) -> ThreadSummaryInfo:
    return ThreadSummaryInfo(
        id=thread.id,
        channel=thread.channel_tentacle_id,
        thread_key=thread.thread_id,
        title=thread_title(thread),
        status=thread.status,
        agent=thread.active_agent_tentacle_id,
        model=thread.active_model,
        updated_at=thread.updated_at,
        message_count=len(thread.messages),
    )


def ledger_entry(message: ThreadMessage) -> LedgerEntry:
    # peek(): the sender profile selectin-loads with the thread's messages, and
    # a serializer must not fire provider IO.
    sender = message.sender.peek()
    return LedgerEntry(
        id=message.id,
        direction=message.direction,
        actor_kind=message.actor_kind,
        agent=message.agent_tentacle_id,
        sender=sender.name if sender is not None else "",
        happened_at=message.happened_at,
        text=message.message_text or "",
    )


def build_trunkline_router(
    octomate: Octomate,
    *,
    channel_id: str = "trunkline",
) -> APIRouter:
    """Build the console HTTP router bound to a registered TrunklineTentacle."""
    registered = octomate.channels.get(channel_id)
    if not isinstance(registered, TrunklineTentacle):
        raise ValueError(
            f"Trunkline router requires a registered TrunklineTentacle at "
            f"{channel_id!r}"
        )
    channel: TrunklineTentacle = registered

    router = APIRouter(prefix="/api/trunkline", tags=["trunkline"])

    @router.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @router.get("/routes")
    async def routes() -> list[RouteInfo]:
        return [
            RouteInfo(
                id=f"{agent_config.agent}{ROUTE_SEP}{agent_config.model}",
                agent=agent_config.agent,
                model=agent_config.model,
            )
            for agent_config in channel.routable_agents()
        ]

    @router.get("/threads")
    async def list_threads() -> list[ThreadSummaryInfo]:
        threads = await octomate.thread_manager.list_threads()
        return [thread_summary(thread) for thread in threads]

    @router.get("/threads/{thread_id}")
    async def thread_detail(thread_id: uuid.UUID) -> ThreadDetailInfo:
        thread = await octomate.thread_manager.get(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        pending = await octomate.deferred_actions.pending_for_thread(thread.id)
        summary = thread_summary(thread)
        return ThreadDetailInfo(
            **dict(summary),
            entries=[ledger_entry(message) for message in thread.messages],
            sessions=[
                SessionEntry(
                    id=handoff.id,
                    from_agent=handoff.from_agent_tentacle_id,
                    to_agent=handoff.to_agent_tentacle_id,
                    model=handoff.to_model,
                    reason=handoff.reason,
                    created_at=handoff.created_at,
                )
                for handoff in thread.handoffs
            ],
            pending=[
                ActionBatchEvent(
                    batch_id=str(batch.id),
                    questions=list(batch.questions),
                    approvals=list(batch.approvals),
                )
                for batch in pending
            ],
        )

    @router.post(
        "/threads/{thread_key}/messages",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Send a directive and stream the run's native events",
        description="`thread_key` is the trunkline platform thread id (not the "
        "row id): a fresh key creates the thread, an existing one continues it.",
    )
    async def send_directive(thread_key: str, body: DirectiveBody) -> StreamingResponse:
        directive = TrunklineDirective(
            thread_id=thread_key,
            text=body.text,
            message_id=body.message_id,
            model=body.model,
        )
        try:
            return await channel.handle_directive(directive)
        except RouteLockedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post(
        "/batches/{batch_id}/resolve",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Answer a deferred-action batch and stream the resumed run",
    )
    async def resolve_batch(
        batch_id: uuid.UUID, body: BatchResponseBody
    ) -> StreamingResponse:
        try:
            batch = await octomate.deferred_actions.get_batch(batch_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if batch.status != "pending":
            # A resolved batch must not resume twice (double-click, retry).
            raise HTTPException(status_code=409, detail=f"batch already {batch.status}")
        return channel.stream_kick(
            DeferredActionBatchResponse(
                batch_id=batch_id,
                responder_id=CONSOLE_USER_ID,
                answers=body.answers,
                approvals=body.approvals,
                allow_session=body.allow_session,
            )
        )

    return router
