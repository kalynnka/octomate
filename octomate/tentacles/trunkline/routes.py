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

Directives create or continue threads on the trunkline channel itself. Directives,
batch responses and thread operations stream native wire events over SSE.

Where the work happened — a thread's project and a run's directory — is read
here and nowhere written: a thread's project is frozen when its row is written,
and both are learned from the session that ran, so no endpoint takes either.

A conversation's approval posture is the exception: it is the console's to
change mid-thread, and the conversation is what
remembers it. It is switched through PATCH, or — while the thread is still being
composed and has no row to switch — carried on the directive that creates it."""

import uuid
from typing import Annotated, NotRequired, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from pydantic_ai.settings import ThinkingEffort
from uuid_utils.compat import uuid7

from octomate.auth import browser_request, current_user
from octomate.base import Octomate
from octomate.config.agents import AgentRouteModelName
from octomate.dependencies import (
    application,
    conversation_manager,
    deferred_action_manager,
    gateway_manager,
    project_manager,
    thread_manager,
    user_manager,
    workspace_manager,
)
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager, GatewayRefusal, OctomateSession
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.agent import AgentInfo
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.operations import ThreadOperations
from octomate.schemas.project import Project
from octomate.schemas.thread import CODEX_NATIVE_ID, Thread, ThreadKey, ThreadMessage
from octomate.schemas.user import ProfileInfo, User, UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.trunkline.base import (
    ROUTE_SEP,
    RouteLockedError,
    TrunklineDirective,
    TrunklineTentacle,
)
from octomate.types.permissions import AgentPermissionMode, PermissionMode


class RouteInfo(BaseModel):
    """One route a directive may name, as `GET /routes` lists it."""

    id: str = Field(description="The route id a directive's `model` field names.")
    agent: str
    model: AgentRouteModelName | None


class ChannelInfo(BaseModel):
    """One connected channel tentacle, as `GET /channels` lists it."""

    id: str = Field(description="The connected channel tentacle's id.")
    kind: str = Field(description="The tentacle class name — SlackTentacle, …")


class DirectiveBody(BaseModel):
    """The body of a directive sent to a thread.

    The text, and the route, project, and posture a thread's first directive may
    pick.
    """

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
        description="A posture from GET /permissions, in the vocabulary of the "
        "agent this thread is routed to, stored on that agent's conversation before "
        "the run. This is how a thread that does not exist yet gets one; an owned "
        "thread's is switched through PATCH /conversations/{id}/permission-mode. "
        "Null asks for no change, and never clears a posture already stored.",
    )


class TeleportBody(TypedDict):
    destination: ChannelAddress
    new_thread: NotRequired[bool]
    hint: Annotated[str, Field(min_length=1, max_length=1_000)]


class SummonBody(TypedDict):
    destination: ChannelAddress
    new_thread: NotRequired[bool]
    agent_id: str
    model: str
    brief: Annotated[str, Field(min_length=1, max_length=8_000)]
    hint: Annotated[str, Field(min_length=1, max_length=1_000)]
    effort: NotRequired[ThinkingEffort | None]


class AgentPostures(BaseModel):
    """An agent's approval modes and default, as `GET /permissions` lists them."""

    modes: tuple[PermissionMode, ...] = Field(
        description="This agent's whole vocabulary, in the order a picker steps "
        "through it."
    )
    default: AgentPermissionMode = Field(
        description="What a conversation of this agent's runs under when it declares "
        "nothing of its own — the configured default, which is what a NULL "
        "`permission_mode` means rather than 'no posture'."
    )


class PermissionModeBody(BaseModel):
    """The body of a PATCH switching a conversation's approval posture."""

    permission_mode: AgentPermissionMode | None = Field(
        default=None,
        description="A posture from GET /permissions, in this conversation's "
        "own agent's vocabulary. Null clears it: nothing is declared and the agent's "
        "configured default decides again.",
    )


class BatchResponseBody(BaseModel):
    """The body resolving a deferred-action batch.

    Answers and approvals keyed by action id, and whether an approved tool stays
    allowed for the rest of the session.
    """

    answers: dict[uuid.UUID, str] = Field(default_factory=dict)
    approvals: dict[uuid.UUID, bool] = Field(default_factory=dict)
    allow_session: bool = False


async def accessible_thread(
    thread_id: uuid.UUID,
    threads: Annotated[ThreadManager, Depends(thread_manager)],
    user: Annotated[User, Depends(current_user)],
) -> Thread:
    thread = await threads.get(thread_id, with_messages=False, user_id=user.id)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
    return thread


async def thread_gateway(
    thread: Annotated[Thread, Depends(accessible_thread)],
    user: Annotated[User, Depends(current_user)],
    app: Annotated[Octomate, Depends(application)],
    gateway: Annotated[GatewayManager, Depends(gateway_manager)],
    actions: Annotated[DeferredActionManager, Depends(deferred_action_manager)],
    users: Annotated[UserManager, Depends(user_manager)],
    threads: Annotated[ThreadManager, Depends(thread_manager)],
    workspaces: Annotated[WorkspaceManager, Depends(workspace_manager)],
) -> OctomateSession:
    agent_id = thread.active_agent_tentacle_id
    if agent_id is None:
        raise HTTPException(409, "This thread has no active agent.")
    if any(gateway.get(c.id) is not None for c in thread.conversations):
        raise HTTPException(409, "Wait for the active turn to finish.")
    if await actions.pending_for_thread(thread.id):
        raise HTTPException(409, "Resolve the pending questions or approvals first.")
    native = thread.kind == "native_thread"
    channel = app.channels.get(thread.channel_tentacle_id)
    if not native and (channel is None or agent_id not in app.agents):
        raise HTTPException(409, "The source channel or agent is not connected.")
    profile = UserProfile(user_id=user.id, channel_user_id=str(user.id), name=user.name)
    if channel is not None and not isinstance(channel, TrunklineTentacle):
        linked = await users.linked_profiles(profile)
        source_profile = next(
            (one for one in linked if one.channel_tentacle_id == channel.id), None
        )
        if source_profile is None:
            raise HTTPException(403, "Link your account on the source channel first.")
        profile = source_profile
    else:
        profile.channel_tentacle_id = thread.channel_tentacle_id
    return OctomateSession(
        channel_routes=gateway.available_routes(app.channels, app.agents),
        current_agent_id=agent_id,
        channels=app.channels,
        agents=app.agents,
        users=users,
        user_profile=profile,
        thread_id=thread.id,
        native=native,
        conversation_address=ChannelAddress(
            channel_tentacle_id=thread.channel_tentacle_id,
            chat_type=thread.chat_type,
            chat_id=thread.chat_id,
            channel_thread_id=thread.channel_thread_id,
            user_id=profile.channel_user_id,
            # External thread rows do not retain their parent surface's privacy.
            # Only known-private sources may export the full history.
            shared=not native
            and not isinstance(channel, TrunklineTentacle)
            and thread.chat_type != "dm",
        ),
        threads=threads,
        workspaces=workspaces,
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

    router = APIRouter(
        prefix="/api/trunkline",
        tags=["trunkline"],
        dependencies=[Depends(browser_request), Depends(current_user)],
    )

    @router.get("/threads/{thread_id}/operations")
    async def thread_operations(
        session: Annotated[OctomateSession, Depends(thread_gateway)],
    ) -> ThreadOperations:
        return await session.operations

    @router.get(
        "/threads/{thread_id}/channels/{channel_id}/addresses",
        summary="List one level of a connected channel's destinations",
    )
    async def list_addresses(
        channel_id: str,
        session: Annotated[OctomateSession, Depends(thread_gateway)],
        inside: Annotated[str | None, Query(max_length=200)] = None,
    ) -> list[ChannelAddress]:
        try:
            return await session.list_addresses(channel_id, inside)
        except GatewayRefusal as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/threads/{thread_id}/teleport")
    async def teleport_thread(
        body: TeleportBody,
        session: Annotated[OctomateSession, Depends(thread_gateway)],
    ) -> StreamingResponse:
        try:
            await session.teleport(**body)
        except GatewayRefusal as exc:
            raise HTTPException(409, str(exc)) from exc
        return channel.stream_kick(session.thread_operation())

    @router.post("/threads/{thread_id}/summon")
    async def summon_thread(
        body: SummonBody,
        session: Annotated[OctomateSession, Depends(thread_gateway)],
    ) -> StreamingResponse:
        try:
            await session.summon(
                agent_id=body["agent_id"],
                model=body["model"],
                destination=body["destination"],
                new_thread=body.get("new_thread", True),
                hint=body["hint"],
                reason="Summon requested from Trunkline",
                summon=body["brief"],
                effort=body.get("effort"),
            )
        except GatewayRefusal as exc:
            raise HTTPException(409, str(exc)) from exc
        return channel.stream_kick(session.thread_operation())

    @router.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @router.get("/routes")
    async def routes() -> list[RouteInfo]:
        return [
            RouteInfo(
                id=f"{agent_config.agent}{ROUTE_SEP}{agent_config.model or ''}",
                agent=agent_config.agent,
                model=agent_config.model,
            )
            for agent_config in channel.routable_agents()
        ]

    @router.get("/projects")
    async def list_projects(
        projects: Annotated[ProjectManager, Depends(project_manager)],
    ) -> list[Project]:
        """The projects a new thread can be filed under — the enabled ones, since
        a project whose root disk has lost is nowhere to work."""
        return [project for project in projects.list() if project.enabled]

    @router.get("/permissions")
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
            if configured is None:
                continue
            agent.check_permission_mode(configured)
            postures[agent_id] = AgentPostures(
                modes=agent.permission_modes, default=configured
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

    @router.get("/agents")
    async def list_agents() -> list[AgentInfo]:
        """The registered agents with their catalogs — every model each one can be
        routed to, what that route claims to be for, and the effort levels it takes.

        `GET /routes` answers the composer's question, which is narrower: the ids a
        directive may name, with the entry agent's default alone. This answers the
        Agents page's question, which is what the instance is made of, so it keeps
        the models that question drops and adds what no route id carries.

        The session counts are the live ones, read off the tentacle as the request
        passes: what this instance is driving now, and what it is only reading.
        """
        return [agent.info for agent in octomate.agents.values()]

    @router.get("/profile")
    async def profile(user: Annotated[User, Depends(current_user)]) -> ProfileInfo:
        """The signed-in user, linked channel profiles, and OAuth MCP authorizations."""
        return await octomate.profile(user)

    @router.get(
        "/threads",
        summary="This user's threads, most recently touched first",
        response_model_exclude={"__all__": {"messages", "parent"}},
    )
    async def list_threads(
        threads: Annotated[ThreadManager, Depends(thread_manager)],
        user: Annotated[User, Depends(current_user)],
    ) -> list[Thread]:
        return await threads.list_threads(user_id=user.id)

    @router.get("/threads/{thread_id}", response_model_exclude={"messages", "parent"})
    async def read_thread(
        thread: Annotated[Thread, Depends(accessible_thread)],
    ) -> Thread:
        """One thread and its handoffs, by row id — any channel's, not only the
        console's own."""
        return thread

    @router.get(
        "/threads/{thread_id}/messages",
        summary="The thread's chat ledger, oldest first",
        response_model_exclude={"__all__": {"model_messages"}},
    )
    async def thread_messages(
        thread_id: uuid.UUID,
        threads: Annotated[ThreadManager, Depends(thread_manager)],
        user: Annotated[User, Depends(current_user)],
    ) -> list[ThreadMessage]:
        thread = await threads.get(thread_id, user_id=user.id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id}")
        return list(thread.messages)

    @router.get(
        "/threads/{thread_id}/conversations",
        summary="The thread's agent conversations, each with its runs",
        response_model_exclude={"__all__": {"messages"}},
    )
    async def thread_conversations(
        thread: Annotated[Thread, Depends(accessible_thread)],
        conversations: Annotated[ConversationManager, Depends(conversation_manager)],
    ) -> list[Conversation]:
        """Subagent conversations included — they name their parent, so a reader
        can fold them under the run whose tool call spawned them.

        Each run carries its model messages, which is where the thinking and the
        tool calls are: the chat ledger holds what was said, and a reader reloading
        a thread would otherwise watch a run's whole middle disappear. The
        conversation's own `messages` stay excluded — that relation is the same rows
        under a different parent, and one copy is enough."""
        return await conversations.for_thread(thread.id, with_run_messages=True)

    @router.get("/threads/{thread_id}/project")
    async def thread_project(
        thread: Annotated[Thread, Depends(accessible_thread)],
    ) -> Project | None:
        """The project this thread's work is in; null for a thread no project
        claims. Frozen: it is set when the thread is created, from the directory
        the session ran in, and no endpoint changes it."""
        return await thread.project

    @router.get(
        "/threads/{thread_id}/batches",
        summary="Unanswered action batches, oldest first",
        response_model_exclude={"__all__": {"requests"}},
    )
    async def thread_batches(
        thread: Annotated[Thread, Depends(accessible_thread)],
        deferred_actions: Annotated[
            DeferredActionManager, Depends(deferred_action_manager)
        ],
    ) -> list[DeferredActionBatch]:
        """The waiting questions and approvals, so a reload re-renders the
        feelers a run is blocked on. `requests` stays behind: it is the agent's
        own tool-call payload, and the actions carry what a reader asks."""
        return await deferred_actions.pending_for_thread(thread.id)

    @router.post(
        "/threads/{thread_key}/messages",
        responses={200: {"content": {"text/event-stream": {}}}},
        summary="Send a directive and stream the run's native events",
        description="`thread_key` is the trunkline platform thread id (not the "
        "row id): a fresh key creates the thread, an existing one continues it.",
    )
    async def send_directive(
        thread_key: str,
        body: DirectiveBody,
        user: Annotated[User, Depends(current_user)],
    ) -> StreamingResponse:
        directive = TrunklineDirective(
            thread_id=thread_key,
            user=user,
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

    @router.post(
        "/threads/{thread_id}/fork",
        summary="Fork a native Codex thread into a new private Trunkline thread",
        response_model_exclude={"messages", "parent"},
        status_code=201,
    )
    async def fork_thread(
        thread: Annotated[Thread, Depends(accessible_thread)],
        user: Annotated[User, Depends(current_user)],
    ) -> Thread:
        if thread.active_agent_tentacle_id != CODEX_NATIVE_ID:
            raise HTTPException(
                status_code=422, detail="Only native Codex threads can be forked here"
            )
        sources = [
            conversation
            for conversation in thread.conversations
            if conversation.agent_tentacle_id == thread.active_agent_tentacle_id
            and not conversation.subagent_id
        ]
        if not sources:
            raise HTTPException(
                status_code=409, detail="The thread has no active conversation to fork"
            )
        agent = next(
            (
                octomate.agents[agent_id]
                for agent_id in channel.agent_ids
                if isinstance(octomate.agents.get(agent_id), CodexTentacle)
            ),
            None,
        )
        if not isinstance(agent, CodexTentacle):
            raise HTTPException(status_code=503, detail="No Codex agent is configured")
        try:
            return await agent.fork(
                sources[-1],
                ThreadKey(channel.id, "thread", str(user.id), uuid7().hex),
                sender=UserProfile(
                    channel_tentacle_id=channel.id,
                    channel_user_id=str(user.id),
                    user_id=user.id,
                    name=user.name,
                    nickname=user.nickname,
                ),
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="No conversation") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.patch(
        "/conversations/{conversation_id}/permission-mode",
        summary="Switch the approval posture this conversation's agent works under",
        response_model_exclude={"messages", "runs"},
    )
    async def set_permission_mode(
        conversation_id: uuid.UUID,
        body: PermissionModeBody,
        threads: Annotated[ThreadManager, Depends(thread_manager)],
        conversations: Annotated[ConversationManager, Depends(conversation_manager)],
        user: Annotated[User, Depends(current_user)],
    ) -> Conversation:
        """The one place a live thread's posture changes. A run reads it as it
        starts, so the switch lands on the next turn and leaves anything in flight
        alone — including a batch already waiting on a human."""
        try:
            conversation = await conversations.get(conversation_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        await accessible_thread(conversation.thread_id, threads, user)
        try:
            agent = octomate.agents.get(conversation.agent_tentacle_id)
            if agent is None:
                raise ValueError(
                    "This conversation has no driven agent to set permissions on"
                )
            if body.permission_mode is not None:
                agent.check_permission_mode(body.permission_mode)
            return await conversations.set_permission_mode(
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
        batch_id: uuid.UUID,
        body: BatchResponseBody,
        user: Annotated[User, Depends(current_user)],
        threads: Annotated[ThreadManager, Depends(thread_manager)],
        conversations: Annotated[ConversationManager, Depends(conversation_manager)],
        deferred_actions: Annotated[
            DeferredActionManager, Depends(deferred_action_manager)
        ],
    ) -> StreamingResponse:
        try:
            batch = await deferred_actions.get_batch(batch_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        conversation = await conversations.get(batch.conversation_id)
        await accessible_thread(conversation.thread_id, threads, user)
        if batch.status != "pending":
            # A resolved batch must not resume twice (double-click, retry).
            raise HTTPException(status_code=409, detail=f"batch already {batch.status}")
        return channel.stream_kick(
            DeferredActionBatchResponse(
                batch_id=batch_id,
                responder_id=str(user.id),
                answers=body.answers,
                approvals=body.approvals,
                allow_session=body.allow_session,
            )
        )

    return router
