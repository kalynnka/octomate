"""The gateway as MCP: the routing spells, served to agent runtimes.

One family on the server `octomate.mcp.server` composes. Its tools are typed functions whose
contracts and schemas are shared by all runtimes, so no two runtimes read two different tools; all that differs is the session
a call runs against, which each tool takes as a FastMCP dependency the mounting side
supplied — `Depends(...)` of a fixed session for a server mounted in-process for one
turn, of a per-request lookup for a server that answers over HTTP.

Refusals are spoken as the model reads them: the `GatewayRefusal` policy raises
becomes a `ToolError` carrying the same sentence, as it becomes a `ModelRetry` for
Inkling, so every runtime corrects from one wording.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import (
    get_access_token,
    get_context,
    get_http_headers,
)
from octomate_protocol.gateway import GatewayTool
from pydantic import Field, TypeAdapter

from octomate.capabilities.harness.events import MessageSentEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.gateway import GatewayRefusal, OctomateSession
from octomate.managers.thread import ThreadManager
from octomate.schemas.awakes import NativeGatewaySignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import (
    DIRECT_TARGET,
    HERE_TARGET,
    InspectFacet,
    SchemeTarget,
    SendTarget,
)
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    # Runtime dependency runs the other way (the host builds and mounts this
    # module's servers); the resolver only needs the host's type here.
    from octomate.base import Octomate
    from octomate.reflex import ReflexGraphResult

# What a runtime a tool result cannot suspend is told: the decision is recorded, its
# turn is interrupted on it, and the graph performs the move and resumes it there.
TELEPORT_RECORDED = (
    "Teleporting — this turn ends here; you continue over there, with your context "
    "intact."
)
# What a native session is told: it approved the call itself, so the move goes
# ahead, carrying its history to a driven session there; this one stays as it is.
NATIVE_TELEPORT_STARTED = (
    "Teleporting — this conversation continues at the destination, its history "
    "with it, in a session Octomate runs there. This session stays as it is."
)

# The header a served call names its turn's conversation with. It comes from a
# launch config Octomate itself wrote, never from the model.
CONVERSATION_HEADER = "X-Octomate-Conversation"

# The header a native session's static MCP config names its configured agent with —
# written once at install time, never per session. Attribution within the bearer's
# trust domain, not authentication: the bearer is what authenticates.
CLIENT_HEADER = "X-Octomate-Client"


GATEWAY_INSTRUCTION_TEMPLATE = """\
## Gateway — decide where this conversation goes and who handles it

These tools route the conversation. Default to handling it yourself: if you can answer
well or do the work, do it and call none of them. Routing is the exception — reach for a
tool only when one of the signals below clearly fires.

### Addresses and discovery
For `{teleport}`, `destination=null` uses the current address.
For `{summon}`, null hands over here; a supplied address starts independent work.
A destination is a `ChannelAddress` object carrying
`channel_tentacle_id`, `chat_type`, `chat_id`, `user_id`, and optional
`channel_thread_id` and `shared` visibility. Use known identifiers and preserve
visibility; `user_id` must match the requesting user's linked identity on that
channel. When no current conversation is attached, supply an explicit address.

`{inspect}` with `reveal="destinations"` suggests addresses without creating anything.
To look further, add `channel` to browse one connected channel a level at a time:
an address with `metadata.inside` is a place to open by passing that value as
`inside`, and one without it can host a new thread unless `metadata.barred` says
why not. A channel that cannot be browsed says so.
You can also supply a known address directly; discovery is optional, and each
operation validates the address independently. Suggestions are not an exhaustive
list or a guarantee that every spell can use each address.
For `{teleport}`, `new_thread=true` is the default. For creation,
use `channel_thread_id=null`; an existing thread cannot contain another thread.

### `{summon}` — hand off to another agent
Without a destination, summon transfers the conversation to a specialist who takes
over this turn *and its follow-ups*: a real, sticky handoff. With a destination,
the specialist starts independent work in a new thread. Summon only when:
- The request needs a capability you lack — e.g. running or editing code in a real
  repository or environment, or a domain another agent is described for.
- It is substantial specialist work another agent would do markedly better, not
  something you can handle from what you already know.

Do NOT summon when:
- You can already answer or do it — length or a technical-sounding topic is not a reason.
- You are only mildly unsure — ask the user a clarifying question instead.
- No route clearly fits — handle it yourself or ask; never summon on a guess.

When one fires, call `{inspect}` with `reveal="routes"` first to see the agents and what
each is for. Every route carries a claim: its ability (what that agent+model is for)
and the effort levels it accepts — pick the route whose ability covers the work. Set
`effort` only when the user explicitly asked for a level; otherwise leave it unset so
the agent's own default applies. Then `{summon}` — copying its `agent_id` and `model`
exactly from that route, and writing a self-contained brief since the other agent may
not see this chat. Omit `destination` to hand over here; this is refused on a
group's main conversation and in native sessions. Supply a parent address to start
independent work there while continuing here, inspecting that channel's routes first.
The call returns the new thread's address without waiting for its result. A `project`
and optional `ref` select its workspace. Native sessions may use this mode without
uploading history. You yourself are not a valid summon target.

The `hint` visibly opens the thread; `brief` is the receiver's self-contained task.
Tell the user where to follow the work. Results and follow-ups belong in that thread;
your current conversation keeps its owner and history. Unsupported or already nested
destinations are refused. Each summon with a destination starts new work;
do not repeat a successful call to check its progress.

### `{teleport}` — relocate yourself
Move this conversation into a new thread that *you* keep handling, carrying
everything said so far. Use it for multi-step or long-running work that deserves its
own thread but that you are the right one to do — no other agent involved.
Omit `destination` or use null for the current address, or supply another address.
Teleport requires attached conversation history that the agent can copy, and the
destination must support continuing that agent. Shared history can only move into
a sub-thread of the current chat; moving to another address requires a private
source. If a requested new thread cannot be created, the move fails without
silently continuing at its parent.

To work on a project, add `project` (from `{inspect}` with `reveal="projects"`), and
`ref` — a branch, tag or commit — only when the default branch is the wrong place to
start: the thread you land in is bound to it and you resume in its workspace, where
work is kept. A thread about no project runs in a throwaway tree, so do this before
you start work, not after. From inside a thread, use `destination=null`,
`new_thread=false` and `project` to bind this one without opening another.
Binding the current thread always resumes your work in its project workspace.
Staying put requires a project; a thread binds once, and a different project is
a different thread. Native sessions must move into a new thread.

### `{scheme}` — take it to the user privately
Continue one-to-one with the person who asked, in their direct messages: for work that
is theirs alone, or that does not belong in front of the group. Whoever already handles
their direct messages picks it up, so write `brief` self-contained — it may not be you,
and they cannot see this chat. `hint` opens the conversation over there and is the
first thing they read; nothing is posted here, so close out your own reply by saying
the work is moving, not what it is. Its destination is a target object with
`kind="dm"`, optionally naming a connected `channel` ID for the user's linked
account there. Omitting the destination uses this channel's DM; that local move
requires a shared source. Another connected channel's DM can also be selected
from a private source. The tool refuses unsupported or unlinked destinations.

### `{dismiss}` — give the workspace back when the work is done
A thread about a project keeps its workspace between turns, which is what makes its
next turn a resume. When the work is finished for good — merged, delivered, or
dropped — say so with `{dismiss}`: the tree is released when this turn ends, after
this turn's work is saved to the project's mirror, and a later message here forks it
afresh from there, so nothing is lost but the disk. Not for a pause — a thread
waiting on someone keeps its tree, and idle ones are swept on their own. Wrap up in
the same turn; your working directory goes with it.
"""

HANDOFF_INSTRUCTION = """

### Writing a brief
A `summon` or `scheme` brief is the receiver's whole opening prompt, and it is
budgeted. Say, in this order and only where it changes what they do next: the goal
in the user's own words; the constraints they set; the decisions made and why; what
is done; what was tried and failed, so it is not tried again; what is left, first
item first; and what a finished result looks like. Leave the rest to the ledger:
the receiver reads every thread this person has spoken in, so cite a message by
its `#msg:<id>` handle and name what to search for instead of repeating it. Do
not restate the project — the workspace's own instructions carry that.
"""

SEND_INSTRUCTION_TEMPLATE = """\

### `{send}` — deliver something now, without ending your turn
For a progress update, an intermediate result, or an image/file you produced along
the way. Anything sent this way is already delivered: your final reply continues from
there — summarize or extend it, never restate it. If everything worth saying went out
already, close with a short wrap-up rather than re-sending it.

`destination` defaults to a target object with `kind="here"`. A target with
`kind="dm"` delivers to the person who asked, privately —
for something that is *for them*, like a summary sent over; say in your reply that you
sent it. `dm` hands nothing over: you keep this conversation and nobody picks the work
up there, so use `{scheme}` when the work itself should continue privately. Asking for
`dm` while already in that person's direct messages is fine — it lands here.
A DM target can name a connected `channel` ID to reach their linked account there.
If no current conversation is attached, that channel must be explicit. Use the
selected address's `channel_tentacle_id` as `channel`; the recipient is resolved
from the user's linked profile.

To thread onto a specific message in a busy chat, lead with a reply segment whose id
is that message's `#msg:<id>` handle — it must be the first segment. To ping someone,
use an `at` segment with their user id.
"""


def gateway_instructions(tool_name: Callable[[GatewayTool], str]) -> str:
    """The gateway's routing instruction, each spell rendered by the caller's own tool
    naming — the identity for Inkling, `gateway_…` on the served server — so every
    agent reads one contract under the names its runtime lists the tools by."""
    names = {
        "inspect": tool_name(GatewayTool.INSPECT),
        "summon": tool_name(GatewayTool.SUMMON),
        "teleport": tool_name(GatewayTool.TELEPORT),
        "scheme": tool_name(GatewayTool.SCHEME),
        "send": tool_name(GatewayTool.SEND),
        "dismiss": tool_name(GatewayTool.DISMISS),
        "commission": tool_name(GatewayTool.COMMISSION),
        "whisper": tool_name(GatewayTool.WHISPER),
    }
    return (
        GATEWAY_INSTRUCTION_TEMPLATE.format(**names)
        + SEND_INSTRUCTION_TEMPLATE.format(**names)
        + COMMISSION_INSTRUCTION_TEMPLATE.format(**names)
    )


COMMISSION_INSTRUCTION_TEMPLATE = """\

### `{commission}` — put another agent to work in the background (you keep the conversation)
Where `{summon}` hands the conversation away, `{commission}` does not: another agent works a
self-contained task and the tool returns its report — the user sees only your reply.
Call `{inspect}` with `reveal="routes"` and no `channel` to choose a route from this
conversation's channel; the same claim and effort rules as `{summon}` apply.
Give the accomplice a short mnemonic `name`. The brief must stand alone: the
accomplice cannot see this chat and has no user to ask, so include the goal, the
relevant context, and what a finished result looks like. Several commissions in one
reply run concurrently.

### `{whisper}` — a quiet word to an accomplice
Continue an accomplice's work by `name`: it remembers everything it did. Use it to
refine or extend that work instead of commissioning a new accomplice.
"""


def spoken[**SpellP, SpellT](
    spell: Callable[SpellP, Awaitable[SpellT]],
) -> Callable[SpellP, Awaitable[SpellT]]:
    """A spell with its refusal spoken as the model reads it: a `ToolError` carrying
    the gateway's sentence verbatim, as `ModelRetry` does for Inkling. A refusal is
    ordinary traffic, not a failure, and is logged as such — anything else that
    escapes a spell is a real error and keeps FastMCP's own handling."""

    @wraps(spell)
    async def cast(*args: SpellP.args, **kwargs: SpellP.kwargs) -> SpellT:
        try:
            return await spell(*args, **kwargs)
        except GatewayRefusal as refusal:
            raise ToolError(str(refusal), log_level=logging.INFO) from refusal

    return cast


def served_session(octomate: Octomate) -> Callable[[], Awaitable[OctomateSession]]:
    """The session a call served over HTTP runs against.

    Every call speaks for the registered user its verified bearer names. A driven
    turn names its conversation with `CONVERSATION_HEADER`, written into its
    launch config by Octomate itself along with the kicker's own secret, and
    resolves to the session registered while that run is in flight — a bearer
    other than the kicker's is refused, so no user can drive another's turn. A
    native session names its agent with `CLIENT_HEADER`, written once at
    install time, and gets `native_session` built for the bearer's user.
    Identity is asserted by config and credential either way, never chosen by
    the model, so a call carrying neither header is refused outright rather than
    guessed at."""

    async def resolve() -> OctomateSession:
        headers = get_http_headers()
        access = get_access_token()
        principal = access.client_id if access is not None else None
        named = headers.get(CONVERSATION_HEADER.lower())
        if named is not None:
            try:
                conversation_id = uuid.UUID(named)
            except ValueError:
                raise ToolError(
                    f"{CONVERSATION_HEADER} is not a conversation id: {named!r}."
                ) from None
            session = octomate.gateway.get(conversation_id)
            if session is None:
                raise ToolError(
                    f"No turn of conversation {named} is at the gateway; a session "
                    "reaches it only while the run that opened it is in flight."
                )
            kicker = (
                await octomate.users.owner(session.user_profile)
                if session.user_profile is not None
                else None
            )
            if kicker is None or kicker.username != principal:
                raise ToolError(
                    f"Conversation {named}'s turn is not this bearer's to drive: "
                    "a driven session speaks with its kicker's own credential, "
                    "which its launch config carries."
                )
            return session
        client = headers.get(CLIENT_HEADER.lower())
        if client is None:
            raise ToolError(
                "This call names no identity: a driven turn names its conversation "
                f"with {CONVERSATION_HEADER}, a native session its agent with "
                f"{CLIENT_HEADER}. Both are written by config, so a call carrying "
                "neither has nothing the gateway may answer for."
            )
        if principal is None:
            raise ToolError(
                "A native session speaks for a registered user, and this call's "
                "bearer names none. Run `octomate configure --token` with "
                "their API token on their machine, then re-run the harness's "
                "`mcp install` command."
            )
        return await native_session(octomate, client, principal)

    return resolve


async def native_session(
    octomate: Octomate, agent_tentacle_id: str, username: str
) -> OctomateSession:
    """An ephemeral gateway for one native call, never registered.

    `username` is the verified bearer's owner, so the session speaks for that
    person: anchored on a transient profile of theirs, its destinations are
    their own linked accounts. The call is attributed to the configured agent, never
    to a terminal session, so the session has no thread and no address — every
    destination is a crossing. A static MCP config puts the tools in every
    session once installed, and the bearer is the only control.
    """
    agent = octomate.agents.get(agent_tentacle_id)
    if agent is None:
        raise ToolError(
            f"{CLIENT_HEADER} names no configured agent: {agent_tentacle_id!r}."
        )
    profile = await octomate.users.native_profile(agent.id, username)
    if profile is None:
        raise RuntimeError(
            f"the bearer verified as {username!r} but the registry holds no such user"
        )
    return OctomateSession(
        channel_routes=octomate.gateway.available_routes(
            octomate.channels, octomate.agents
        ),
        current_agent_id=agent.id,
        channels=octomate.channels,
        users=octomate.users,
        user_profile=profile,
        agents=octomate.agents,
        native=True,
        threads=octomate.threads,
        workspaces=octomate.workspaces,
        nerve=octomate.nerve,
    )


def caller_thread() -> str | None:
    """The native session a call came from, where its runtime says so itself:
    Codex names its thread in every call's `_meta`."""
    request = get_context().request_context
    meta = request.meta if request is not None else None
    thread_id = (meta or {}).get("threadId")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def mount_gateway(
    mcp: FastMCP,
    octomate_session: OctomateSession,
    thread_manager: ThreadManager,
    kick: Callable[
        [NativeGatewaySignal], Awaitable[asyncio.Task[ReflexGraphResult] | None]
    ]
    | None = None,
    *,
    conversations: ConversationManager,
) -> None:
    """Register the gateway's spells on `mcp`.

    `octomate_session` is the FastMCP dependency each call resolves its session
    through — `Depends(...)` of a fixed session for a server mounted in-process for
    one turn, of a per-request lookup for a server that answers over HTTP.
    `thread_manager` is the ledger a delivering spell writes through. `kick` is how
    a native session's teleport or scheme becomes its own turn at once, so only the
    served mount — the one place a native session can arrive — needs one.
    """

    @mcp.tool(
        name=GatewayTool.INSPECT,
        annotations={"readOnlyHint": True},
    )
    @spoken
    async def inspect(
        reveal: InspectFacet,
        channel: str | None = None,
        inside: str | None = None,
        session: OctomateSession = octomate_session,
    ) -> str:
        """Reveal one facet of what this conversation can reach.

        Args:
            reveal: `routes` — agent/model choices on the current or selected channel.
                In-place summon and commission use the current channel's routes;
                summon with a destination uses that channel's routes.
                `destinations` — optional address suggestions, validated independently
                when used; known addresses need not appear in this list. With
                `channel`, one level of that channel's addresses instead.
                `projects` — the projects this deployment can work on, for `teleport`.
            channel: A connected channel ID from an address. For `routes`, omit it
                to inspect the current conversation's routes. For `destinations`,
                name it to browse that channel as the requesting user: an address
                whose `metadata.inside` is set is a place to open, one without it
                can host a new thread unless `metadata.barred` says why not.
            inside: For `destinations` with `channel`, the `metadata.inside` of a
                listed address, to list what that place holds. Omit it for the
                channel's top level.
        """
        if reveal == "destinations":
            addresses = await session.inspect("destinations", channel, inside)
            return (
                TypeAdapter(list[ChannelAddress]).dump_json(addresses).decode()
                if addresses
                else "- (none)"
            )
        # Lines, never the list: FastMCP renders an empty list as no content at all.
        return (
            "\n".join(
                str(one) for one in await session.inspect(reveal, channel, inside)
            )
            or "- (none)"
        )

    @mcp.tool(name=GatewayTool.SUMMON)
    @spoken
    async def summon(
        agent_id: str,
        model: str,
        hint: str,
        reason: str,
        brief: Annotated[str, Field(max_length=8_000)],
        effort: str | None = None,
        destination: ChannelAddress | None = None,
        project: str | None = None,
        ref: str | None = None,
        session: OctomateSession = octomate_session,
    ) -> str | ChannelAddress:
        """Hand over here, or start another agent in a new independent thread.

        Omit destination to hand over this conversation when the turn ends.
        Supply a parent address to open independent work immediately and return
        its thread address without waiting for a result. The current conversation
        keeps its agent, history and pending decision. Native sessions may use
        a destination without uploading history.

        Args:
            agent_id: The target agent, copied exactly from an `inspect` route
                (`reveal="routes"`).
            model: That route's model, copied exactly.
            hint: A short, user-facing note announcing the handoff, recorded with it.
            reason: One line on why this agent fits — recorded with the handoff, not
                shown to the user as the reply.
            brief: The self-contained brief the other agent starts from. It becomes
                their opening prompt and they cannot see this conversation, so give
                the goal, the relevant context and decisions, what's been tried, and
                what a finished result looks like. If anything you carry says how to
                hand off — a handoff skill, a plugin, your own harness's instructions
                — follow it; this is the floor. Reference rather than repeat: the
                receiver reads every thread this person has spoken in, so cite a
                message by its `#msg:<id>` handle and say what to search for instead
                of pasting it. A brief over the size budget is refused, never trimmed.
            effort: How hard the agent should think, from the effort levels the
                route's claim offers. Set it only when the user explicitly asked
                for a level; omitted, the agent's own default applies.
            destination: A parent address at which to create a new thread. Use
                this channel's inspected routes and the requester's linked identity.
                Omit for an in-place handoff. Existing threads cannot be parents.
            project: An enabled project for the new thread; requires a destination
                and a registered user. Omit for a throwaway workspace.
            ref: A starting branch, tag or commit for that project; requires project.
        """
        return await session.summon(
            agent_id=agent_id,
            model=model,
            hint=hint,
            reason=reason,
            brief=brief,
            effort=effort,
            destination=destination,
            project=project,
            ref=ref,
        )

    @mcp.tool(name=GatewayTool.TELEPORT)
    @spoken
    async def teleport(
        hint: str,
        destination: ChannelAddress | None = None,
        project: str | None = None,
        ref: str | None = None,
        new_thread: bool = True,
        resume: bool = False,
        session_id: Annotated[
            str | None,
            Field(
                description="Filled in by Octomate's own hook for a session you "
                "were not started by Octomate in; leave it out."
            ),
        ] = None,
        session: OctomateSession = octomate_session,
    ) -> str | JsonObject:
        """Continue this conversation yourself somewhere else; everything said so
        far comes with you. This turn ends on it, and your context is intact
        there.

        Args:
            hint: The short, user-facing thread-starter message.
            destination: The address at which to create a thread; omit for the
                current chat. The owning channel validates it independently of
                discovery and requires the requesting user's linked identity.
                Shared history stays under its current parent. Teleport requires
                attached history that the agent can copy.
            new_thread: Create a thread before continuing. Set false only to bind
                the current thread to a project without opening another one.
                Binding the current thread always resumes your work.
                For creation, the address must have no `channel_thread_id`.
            project: A project's name, copied exactly from `inspect`
                (`reveal="projects"`), for a conversation about no project: the
                thread you land in is bound to it, and you resume in its workspace,
                where work is kept. A conversation about a project carries it
                wherever it goes, so omit this there.
            ref: The branch, tag or commit that workspace starts from; omit it for
                the project's default branch.
            resume: Carry on working in a new thread at once, as after any tool
                call. Leave it false when the move is all that was asked: you
                continue when the user next writes there. Ignored when
                new_thread is false: binding the current thread always resumes.
        """
        native_session_id = (session_id or caller_thread()) if session.native else None
        if native_session_id is not None:
            await session.attach_native_thread(native_session_id)
        decision = await session.teleport(
            hint=hint,
            destination=destination,
            new_thread=new_thread,
            project=project,
            ref=ref,
            resume=resume,
        )
        request = get_context().request_context
        if (
            not session.native
            and request is not None
            and request.meta is not None
            and request.meta.get("defer_teleport") is True
        ):
            return decision.metadata()
        if not session.native:
            return TELEPORT_RECORDED
        if kick is None:
            raise RuntimeError(
                "a native session reached a gateway mounted without a kick"
            )
        await kick(session.native_handoff())
        return NATIVE_TELEPORT_STARTED

    @mcp.tool(name=GatewayTool.SCHEME)
    @spoken
    async def scheme(
        hint: str,
        brief: Annotated[str, Field(max_length=8_000)],
        destination: SchemeTarget = DIRECT_TARGET,
        session: OctomateSession = octomate_session,
    ) -> str:
        """Continue this with the user one-to-one, in their direct messages.

        Whoever already handles that user's direct messages picks the work up — you do
        not choose them, and their direct messages keep the owner they had. Use it for
        work that is that person's alone, or that does not belong in front of the group.

        Args:
            hint: The short, user-facing line that opens the conversation over there,
                where they will read it before anything else. Say that the work is
                being picked up — not the brief, which is written for whoever answers.
            brief: The self-contained brief whoever answers there starts from. They
                cannot see this conversation, so give the goal, the relevant context and
                decisions, what's been tried, and what a finished result looks like. If
                anything you carry says how to hand off — a handoff skill, a plugin,
                your own harness's instructions — follow it; this is the floor.
                Reference rather than repeat: the receiver reads every thread this
                person has spoken in, so cite a message by its `#msg:<id>` handle and
                say what to search for instead of pasting it. A brief over the size
                budget is refused, never trimmed.
            destination: A target object with `kind="dm"`. Omit `channel` for this
                channel's DM when moving from a shared source, or name another
                connected channel ID, including from a private source. The user's
                linked profile determines the recipient.
        """
        sentence = await session.scheme(hint=hint, brief=brief, destination=destination)
        if session.native:
            if kick is None:
                raise RuntimeError(
                    "a native session reached a gateway mounted without a kick"
                )
            await kick(session.native_handoff())
        return sentence

    @mcp.tool(name=GatewayTool.SEND)
    @spoken
    async def send(
        segments: list[MessageSegment],
        destination: SendTarget = HERE_TARGET,
        session: OctomateSession = octomate_session,
    ) -> str:
        """Deliver these segments immediately, without ending your turn — a progress
        update, an intermediate result, an image or a file. What you send here is
        already delivered: do NOT repeat it in your final reply.

        Args:
            segments: What to deliver.
            destination: A target object with `kind="here"` (the default) or
                `kind="dm"`. A DM target can name a connected `channel` ID; omit
                it for this channel. With no current conversation, name a channel
                explicitly. When choosing from an inspected address, use its
                `channel_tentacle_id` as `channel`; the linked profile supplies the
                recipient. Say in your reply when you sent it elsewhere.
        """
        address = await session.resolve_send(destination)
        target = address if address is not None else session.conversation_address
        if target is None:
            raise GatewayRefusal(
                "This session has no conversation of its own to land a send on — "
                'use a destination with kind="dm" and an explicit connected channel ID.'
            )
        notice = "sent"
        private = False
        if address is not None:
            channel = session.channels[target.channel_tentacle_id]
            dm = await channel.open_dm(target.user_id)
            if dm is not None:
                target = dm
                private = True
            else:
                fallback = session.conversation_address
                if fallback is None:
                    raise GatewayRefusal(
                        f"{channel.name} could not open a direct message with "
                        f"{target.user_id!r}; nothing was delivered."
                    )
                # Mirror React's own fallback: land it where the run lives, and say so.
                target = fallback
                notice = (
                    f"could not open their direct messages on {channel.name}; "
                    "delivered to this conversation instead"
                )
        channel = session.channels[target.channel_tentacle_id]
        message = await thread_manager.record_outbound(
            target,
            agent_tentacle_id=session.current_agent_id,
            segments=segments,
            sender=channel.self_profile,
        )
        sent = MessageSentEvent(
            segments=segments, destination=target, thread_message_id=message.id
        )
        await session.publish(target, sent, private=private)
        return notice

    @mcp.tool(name=GatewayTool.DISMISS)
    @spoken
    async def dismiss(session: OctomateSession = octomate_session) -> str:
        """Give this thread's workspace back, now that the work in it is done —
        merged, delivered, or dropped for good, not paused. It is released when
        this turn ends, after this turn's work is saved to the project's mirror,
        so nothing is lost: a later message on this thread forks it afresh from
        there. Only the disk goes, and it goes now rather than after the idle
        window the sweep would otherwise wait out.
        """
        return await session.dismiss()

    def parent_call(session: OctomateSession) -> str | None:
        if session.conversation_id is None:
            raise ToolError("Accomplice tools require an active conversation.")
        request = get_context().request_context
        tool_call_id = (
            request.meta.get("tool_call_id")
            if request is not None and request.meta is not None
            else None
        )
        if tool_call_id is not None and (
            not isinstance(tool_call_id, str) or not tool_call_id
        ):
            raise ToolError(
                "The tool call id in request metadata must be a non-empty string."
            )
        return tool_call_id

    @mcp.tool(name=GatewayTool.COMMISSION)
    @spoken
    async def commission(
        name: str,
        agent_id: str,
        model: str,
        brief: str,
        effort: str | None = None,
        session: OctomateSession = octomate_session,
    ) -> str:
        """Draw another Octomate agent into working a task and get its report
        back — the user sees none of it.

        Args:
            name: Your name for this accomplice — short and mnemonic, e.g.
                `repo-audit`. `whisper` to it later to follow up.
            agent_id: The agent to draw in, copied exactly from an
                `inspect` route for this conversation's channel (omit `channel`).
            model: That route's model, copied exactly.
            brief: The self-contained work order. The accomplice cannot see
                this conversation and has no user to ask, so give the
                goal, the relevant context and decisions, and what a
                finished result looks like.
            effort: How hard the accomplice should think, from the levels the
                route's claim offers. Set it only when the user
                explicitly asked for a level.
        """
        tool_call_id = parent_call(session)
        assert session.conversation_id is not None
        return await session.commission(
            conversations=conversations,
            parent_id=session.conversation_id,
            parent_run_id=None,
            parent_tool_call_id=tool_call_id,
            name=name,
            agent_id=agent_id,
            model=model,
            brief=brief,
            effort=effort,
        )

    @mcp.tool(name=GatewayTool.WHISPER)
    @spoken
    async def whisper(
        name: str,
        message: str,
        session: OctomateSession = octomate_session,
    ) -> str:
        """A quiet word to an accomplice you commissioned. It keeps
        everything it did — your whisper continues its work, not a fresh
        start.

        Args:
            name: The name you gave the accomplice when you commissioned it.
            message: The follow-up work order — self-contained, like a
                brief; the accomplice still cannot see this conversation.
        """
        tool_call_id = parent_call(session)
        assert session.conversation_id is not None
        return await session.whisper(
            conversations=conversations,
            parent_id=session.conversation_id,
            parent_run_id=None,
            parent_tool_call_id=tool_call_id,
            name=name,
            message=message,
        )
