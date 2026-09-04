"""The Trunkline console HTTP surface, mounted under `/api/trunkline`.

The console is an entry and a reader, and it reads the domain in the shape the
domain has: a thread, the conversations under it, the runs under those, and the
thread's chat messages. Every read answers with the transmuter itself rather
than a console-shaped copy of it, so the payload is the row.

What a read leaves out it leaves out deliberately. A thread's messages are
their own request, because a listing that carried every thread's ledger would
be the ledger; and a run's transcript never leaves at all — the model messages
are the agent's own history, which the history tool owns, not a reader's.

Both halves of leaving something out are load-bearing. `response_model_exclude`
keeps a relation out of the payload; it does nothing about the query, and every
relation named here is lazy="selectin". The managers suppress the load itself
(`ThreadManager.get(with_messages=...)`, `ConversationManager.for_thread`), so
these reads do not fetch a ledger to drop it.

Every read under a thread reads the thread first, and 404s on an id the relay
does not know — a sub-resource of a thread that does not exist must say so
rather than answer the empty list its query would return. Only the ledger read
asks for the messages; the rest want the row.

Directives create or continue threads on the trunkline channel itself. The two
POST endpoints stream: a directive streams its run, and a batch response
streams the resumed run, both as SSE of the native wire events (see `wire`).

Where the work happened — a thread's project and a run's directory — is read
here and nowhere written: a thread's project is frozen when its row is written,
and both are learned from the session that ran, so no endpoint takes either.

A conversation's approval posture is the exception, and the only write under a
thread: it is the console's to change, mid-thread, and the conversation is what
remembers it. It is switched through PATCH, or — while the thread is still being
composed and has no row to switch — carried on the directive that creates it."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from octomate.config.agents import AgentRouteModelName
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import Conversation
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread, ThreadMessage
from octomate.tentacles.trunkline.base import (
    CONSOLE_USER_ID,
    ROUTE_SEP,
    RouteLockedError,
    TrunklineDirective,
    TrunklineTentacle,
)
from octomate.types.permissions import PERMISSION_MODES, AgentPermissionMode

if TYPE_CHECKING:
    from octomate import Octomate


class RouteInfo(BaseModel):
    id: str = Field(description="The route id a directive's `model` field names.")
    agent: str
    model: AgentRouteModelName


class ChannelInfo(BaseModel):
    id: str = Field(description="The connected channel tentacle's id.")
    kind: str = Field(description="The tentacle class name — SlackTentacle, …")


class DirectiveBody(BaseModel):
    text: str
    message_id: str | None = None
    model: str | None = Field(
        default=None,
        description="A route id from GET /routes ('agent:model'). Honored only "
        "on a thread's first directive; a differing pick on an owned thread is "
        "refused with 409 (re-routing needs a manual handoff).",
    )
    project: str | None = Field(
        default=None,
        description="A project name from GET /projects, filing the thread under "
        "it. Honored only on a thread's first directive, since a thread's project "
        "is frozen when the row is written; null is a real answer, and leaves the "
        "thread a chat that is in no project at all.",
    )
    permission_mode: AgentPermissionMode | None = Field(
        default=None,
        description="A posture from GET /permission-modes, in the vocabulary of the "
        "agent this thread is routed to, stored on that agent's conversation before "
        "the run. This is how a thread that does not exist yet gets one; an owned "
        "thread's is switched through PATCH /conversations/{id}/permission-mode. "
        "Null asks for no change, and never clears a posture already stored.",
    )


class AgentPostures(BaseModel):
    modes: list[AgentPermissionMode] = Field(
        description="This agent's whole vocabulary, in the order a picker steps "
        "through it."
    )
    default: AgentPermissionMode = Field(
        description="What a conversation of this agent's runs under when it declares "
        "nothing of its own — the configured default, which is what a NULL "
        "`permission_mode` means rather than 'no posture'."
    )


class PermissionModeBody(BaseModel):
    permission_mode: AgentPermissionMode | None = Field(
        default=None,
        description="A posture from GET /permission-modes, in this conversation's "
        "own agent's vocabulary. Null clears it: nothing is declared and the agent's "
        "configured default decides again.",
    )


class BatchResponseBody(BaseModel):
    answers: dict[uuid.UUID, str] = Field(default_factory=dict)
    approvals: dict[uuid.UUID, bool] = Field(default_factory=dict)
    allow_session: bool = False


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

    @router.get("/projects")
    async def list_projects() -> list[Project]:
        """The projects a new thread can be filed under — the enabled ones, since
        a project whose root disk has lost is nowhere to work."""
        return [project for project in octomate.projects.list() if project.enabled]

    @router.get("/permission-modes")
    async def permission_modes() -> dict[str, AgentPostures]:
        """Each registered agent's approval vocabulary, in the order a picker steps
        through it — a provider's own scale, never a shared one, so the list a
        conversation may be given is keyed by the agent that reads it.

        Only the agents this instance runs: a posture is stored on a conversation,
        and an agent nobody can be routed to has no conversation to store one on. The
        tailed runtimes are absent for the same reason, which is what keeps the console
        reporting their posture rather than offering to switch it."""
        postures: dict[str, AgentPostures] = {}
        for agent_id, agent in octomate.agents.items():
            configured = agent.default_permission_mode
            if agent_id not in PERMISSION_MODES or configured is None:
                continue
            if configured not in PERMISSION_MODES[agent_id]:
                raise ValueError(
                    f"agent {agent_id!r} is configured with {configured!r}, which is "
                    f"not one of its own postures"
                )
            postures[agent_id] = AgentPostures(
                modes=list(PERMISSION_MODES[agent_id]), default=configured
            )
        return postures

    @router.get("/channels")
    async def list_channels() -> list[ChannelInfo]:
        """The channels this instance actually connected — the sidebar's rail
        mirrors this, so a channel disabled in config never appears."""
        return [
            ChannelInfo(id=channel_id, kind=type(tentacle).__name__)
            for channel_id, tentacle in octomate.channels.items()
        ]

    @router.get(
        "/threads",
        summary="Every channel's threads, most recently touched first",
        response_model_exclude={"__all__": {"messages"}},
    )
    async def list_threads() -> list[Thread]:
        return await octomate.thread_manager.list_threads()

    @router.get("/threads/{thread_id}", response_model_exclude={"messages"})
    async def read_thread(thread_id: uuid.UUID) -> Thread:
        """One thread and its handoffs, by row id — any channel's, not only the
        console's own."""
        thread = await octomate.thread_manager.get(thread_id, with_messages=False)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        return thread

    @router.get(
        "/threads/{thread_id}/messages",
        summary="The thread's chat ledger, oldest first",
        response_model_exclude={"__all__": {"model_messages"}},
    )
    async def thread_messages(thread_id: uuid.UUID) -> list[ThreadMessage]:
        thread = await octomate.thread_manager.get(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        return list(thread.messages)

    @router.get(
        "/threads/{thread_id}/conversations",
        summary="The thread's agent conversations, each with its runs",
        response_model_exclude={"__all__": {"messages"}},
    )
    async def thread_conversations(thread_id: uuid.UUID) -> list[Conversation]:
        """Subagent conversations included — they name their parent, so a reader
        can fold them under the run whose tool call spawned them.

        Each run carries its model messages, which is where the thinking and the
        tool calls are: the chat ledger holds what was said, and a reader reloading
        a thread would otherwise watch a run's whole middle disappear. The
        conversation's own `messages` stay excluded — that relation is the same rows
        under a different parent, and one copy is enough."""
        if await octomate.thread_manager.get(thread_id, with_messages=False) is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        return await octomate.conversations.for_thread(
            thread_id, with_run_messages=True
        )

    @router.get("/threads/{thread_id}/project")
    async def thread_project(thread_id: uuid.UUID) -> Project | None:
        """The project this thread's work is in; null for a thread no project
        claims. Frozen: it is set when the thread is created, from the directory
        the session ran in, and no endpoint changes it."""
        thread = await octomate.thread_manager.get(thread_id, with_messages=False)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        return await thread.project

    @router.get(
        "/threads/{thread_id}/batches",
        summary="Unanswered action batches, oldest first",
        response_model_exclude={"__all__": {"requests"}},
    )
    async def thread_batches(thread_id: uuid.UUID) -> list[DeferredActionBatch]:
        """The waiting questions and approvals, so a reload re-renders the
        feelers a run is blocked on. `requests` stays behind: it is the agent's
        own tool-call payload, and the actions carry what a reader asks."""
        if await octomate.thread_manager.get(thread_id, with_messages=False) is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        return await octomate.deferred_actions.pending_for_thread(thread_id)

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
            project=body.project,
            permission_mode=body.permission_mode,
        )
        try:
            return await channel.handle_directive(directive)
        except RouteLockedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.patch(
        "/conversations/{conversation_id}/permission-mode",
        summary="Switch the approval posture this conversation's agent works under",
        response_model_exclude={"messages", "runs"},
    )
    async def set_permission_mode(
        conversation_id: uuid.UUID, body: PermissionModeBody
    ) -> Conversation:
        """The one place a live thread's posture changes. A run reads it as it
        starts, so the switch lands on the next turn and leaves anything in flight
        alone — including a batch already waiting on a human."""
        try:
            conversation = await octomate.conversations.get(conversation_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        try:
            return await octomate.conversations.set_permission_mode(
                conversation, body.permission_mode
            )
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
