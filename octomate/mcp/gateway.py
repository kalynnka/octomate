"""The gateway as MCP: the routing spells, served to every runtime that is not Inkling.

One family on the server `octomate.mcp.server` composes. Its tools are typed functions whose
contracts are Inkling's own tool docstrings and whose schemas come from the same
shapes, so no two runtimes read two different tools; all that differs is the session
a call runs against, which each tool takes as a FastMCP dependency the mounting side
supplied — `Depends(...)` of a fixed session for a server mounted in-process for one
turn, of a per-request lookup for a server that answers over HTTP.

Refusals are spoken as the model reads them: the `GatewayRefusal` policy raises
becomes a `ToolError` carrying the same sentence, as it becomes a `ModelRetry` for
Inkling, so every runtime corrects from one wording.
"""

from __future__ import annotations

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
from pydantic_ai.settings import ThinkingEffort

from octomate.capabilities.gateway import GatewayCapability
from octomate.managers.gateway import GatewayRefusal, OctomateSession
from octomate.managers.thread import ThreadManager
from octomate.mcp.base import capability_contract
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
from octomate.types.threads import NATIVE_TENTACLE_IDS

if TYPE_CHECKING:
    # Runtime dependency runs the other way (the host builds and mounts this
    # module's servers); the resolver only needs the host's type here.
    from octomate.base import Octomate

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

# The header a native session's static MCP config attributes its runtime with —
# written once at install time, never per session. Attribution within the bearer's
# trust domain, not authentication: the bearer is what authenticates.
CLIENT_HEADER = "X-Octomate-Client"


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
    native session names its runtime with `CLIENT_HEADER`, written once at
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
                f"with {CONVERSATION_HEADER}, a native session its runtime with "
                f"{CLIENT_HEADER}. Both are written by config, so a call carrying "
                "neither has nothing the gateway may answer for."
            )
        if client not in NATIVE_TENTACLE_IDS:
            raise ToolError(
                f"{CLIENT_HEADER} names no native runtime: {client!r}. An install "
                f"writes one of: {', '.join(sorted(NATIVE_TENTACLE_IDS))}."
            )
        if principal is None:
            agent = client.removesuffix("-native")
            raise ToolError(
                "A native session speaks for a registered user, and this call's "
                "bearer names none. Run `octomate configure --token` with "
                "their API token on their machine, then re-run "
                f"`octomate {agent} mcp "
                "install`."
            )
        return await native_session(octomate, client, principal)

    return resolve


async def native_session(
    octomate: Octomate, client: str, username: str
) -> OctomateSession:
    """An ephemeral gateway for one native call, never registered.

    `username` is the verified bearer's owner, so the session speaks for that
    person: anchored on a transient profile of theirs, its destinations are
    their own linked accounts. The call is still attributed to a runtime, never
    to a terminal session, so the session has no thread and no address — every
    destination is a crossing. A static MCP config puts the tools in every
    session once installed, and the bearer is the only control.
    """
    profile = await octomate.users.native_profile(client, username)
    if profile is None:
        raise RuntimeError(
            f"the bearer verified as {username!r} but the registry holds no such user"
        )
    return OctomateSession(
        channel_routes=octomate.gateway.available_routes(
            octomate.channels, octomate.agents
        ),
        current_agent_id=client,
        channels=octomate.channels,
        users=octomate.users,
        user_profile=profile,
        agents=octomate.agents,
        native=True,
        threads=octomate.threads,
        workspaces=octomate.workspaces,
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
    kick: Callable[[NativeGatewaySignal], None] | None = None,
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
        description=capability_contract(GatewayCapability.inspect),
        annotations={"readOnlyHint": True},
    )
    @spoken
    async def inspect(
        reveal: InspectFacet,
        channel: str | None = None,
        inside: str | None = None,
        session: OctomateSession = octomate_session,
    ) -> str:
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

    @mcp.tool(
        name=GatewayTool.SUMMON,
        description=capability_contract(GatewayCapability.summon),
    )
    @spoken
    async def summon(
        agent_id: str,
        model: str,
        hint: str,
        reason: str,
        summon: Annotated[str, Field(max_length=8_000)],
        effort: ThinkingEffort | None = None,
        session: OctomateSession = octomate_session,
    ) -> str:
        return await session.summon(
            agent_id=agent_id,
            model=model,
            hint=hint,
            reason=reason,
            summon=summon,
            effort=effort,
        )

    @mcp.tool(
        name=GatewayTool.TELEPORT,
        description=capability_contract(GatewayCapability.teleport),
    )
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
    ) -> str:
        native_session_id = (session_id or caller_thread()) if session.native else None
        if native_session_id is not None:
            await session.attach_native_thread(native_session_id)
        await session.teleport(
            hint=hint,
            destination=destination,
            new_thread=new_thread,
            project=project,
            ref=ref,
            resume=resume,
        )
        if not session.native:
            return TELEPORT_RECORDED
        if kick is None:
            raise RuntimeError(
                "a native session reached a gateway mounted without a kick"
            )
        kick(session.native_handoff())
        return NATIVE_TELEPORT_STARTED

    @mcp.tool(
        name=GatewayTool.SCHEME,
        description=capability_contract(GatewayCapability.scheme),
    )
    @spoken
    async def scheme(
        hint: str,
        brief: Annotated[str, Field(max_length=8_000)],
        destination: SchemeTarget = DIRECT_TARGET,
        session: OctomateSession = octomate_session,
    ) -> str:
        sentence = await session.scheme(hint=hint, brief=brief, destination=destination)
        if session.native:
            if kick is None:
                raise RuntimeError(
                    "a native session reached a gateway mounted without a kick"
                )
            kick(session.native_handoff())
        return sentence

    @mcp.tool(
        name=GatewayTool.SEND, description=capability_contract(GatewayCapability.send)
    )
    @spoken
    async def send(
        segments: list[MessageSegment],
        destination: SendTarget = HERE_TARGET,
        session: OctomateSession = octomate_session,
    ) -> str:
        # Delivered right here: a gateway `send` has no run stream to ride, so the
        # delivery React performs for Inkling's sends happens in the call instead —
        # the same resolve, the same DM open, the same ledger row.
        address = await session.resolve_send(destination)
        target = address if address is not None else session.conversation_address
        if target is None:
            raise GatewayRefusal(
                "This session has no conversation of its own to land a send on — "
                'use a destination with kind="dm" and an explicit connected channel ID.'
            )
        notice = "sent"
        if address is not None:
            channel = session.channels[target.channel_tentacle_id]
            dm = await channel.open_dm(target.user_id)
            if dm is not None:
                target = dm
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
        await channel.feelers.segments.present(target, segments)
        await thread_manager.record_outbound(
            target,
            agent_tentacle_id=session.current_agent_id,
            segments=segments,
            sender=channel.self_profile,
        )
        return notice

    @mcp.tool(
        name=GatewayTool.DISMISS,
        description=capability_contract(GatewayCapability.dismiss),
    )
    @spoken
    async def dismiss(session: OctomateSession = octomate_session) -> str:
        return await session.dismiss()
