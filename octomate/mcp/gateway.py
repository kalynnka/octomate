"""The gateway as MCP: the routing spells, served to every runtime that is not Inkling.

One FastMCP server, built by whoever mounts it. Its tools are typed functions whose
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
from inspect import cleandoc
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_headers
from pydantic_ai.settings import ThinkingEffort

from octomate.capabilities.gateway import GatewayCapability, gateway_instructions
from octomate.managers.gateway import GatewayRefusal, GatewaySession
from octomate.managers.thread import ThreadManager
from octomate.schemas.awakes import GatewayHandoffSignal
from octomate.schemas.messages import SEND_TOOL_NAME
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import (
    DIRECT_TARGET,
    HERE_TARGET,
    SCHEME_TOOL_NAME,
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_TOOL_NAME,
    THREAD_TARGET,
    SchemeTarget,
    SendTarget,
    SummonTarget,
    TeleportTarget,
)
from octomate.types.threads import (
    CLAUDE_NATIVE_ID,
    CODEX_NATIVE_ID,
    DEEPSEEK_NATIVE_ID,
    NATIVE_TENTACLE_IDS,
)

if TYPE_CHECKING:
    # Runtime dependency runs the other way (the host builds and mounts this
    # module's servers); the resolver only needs the host's type here.
    from octomate.base import Octomate

# The MCP server name every runtime mounts the gateway under. Claude and dsh name a
# server's tools `mcp__<server>__<tool>`, Codex namespaces them `mcp__<server>`.
GATEWAY_SERVER_NAME = "gateway"

# The spells the gateway offers, in the order it registers them. `commission` and
# `whisper` are deliberately absent: external runtimes bring their own subagents.
GATEWAY_SPELLS: tuple[str, ...] = (
    SCRY_TOOL_NAME,
    SUMMON_TOOL_NAME,
    TELEPORT_TOOL_NAME,
    SCHEME_TOOL_NAME,
    SEND_TOOL_NAME,
)

# The routing contract under the tools' bare names, which is how a runtime that
# namespaces a server's tools reads them. It goes on the server rather than into a
# prompt because a runtime that defers MCP tools behind a search shows the server's
# own instructions as the namespace's card.
GATEWAY_SERVER_INSTRUCTIONS = gateway_instructions(lambda name: name)

# What a runtime that cannot be suspended mid-run is told: the decision is recorded,
# the graph moves the conversation once its turn ends — so close out, not carry on.
TELEPORT_RECORDED = (
    "Teleporting — wrap up your reply; the move happens after this turn."
)

# The header a served call names its turn's conversation with. It comes from a
# launch config Octomate itself wrote, never from the model.
CONVERSATION_HEADER = "X-Octomate-Conversation"

# The header a native session's static MCP config attributes its runtime with —
# written once at install time, never per session. Attribution within the bearer's
# trust domain, not authentication: the bearer is what authenticates.
CLIENT_HEADER = "X-Octomate-Client"

SpellP = ParamSpec("SpellP")
SpellT = TypeVar("SpellT")


def capability_contract(spell: Callable[..., Awaitable[SpellT]]) -> str:
    """The docstring Inkling's toolset compiles, verbatim — a copy here would give
    two models two different tools and drift silently."""
    doc = spell.__doc__
    if doc is None:
        raise RuntimeError(f"{spell.__qualname__} has no docstring to project")
    return cleandoc(doc)


def spoken(
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


def served_session(octomate: Octomate) -> Callable[[], Awaitable[GatewaySession]]:
    """The session a call served over HTTP runs against.

    Every call speaks for the registered user its verified bearer names. A driven
    turn names its conversation with `CONVERSATION_HEADER`, written into its
    launch config by Octomate itself along with the kicker's own secret, and
    resolves to the session registered while that run is in flight — a bearer
    other than the kicker's is refused, so no user can drive another's turn. A
    native session names its runtime with `CLIENT_HEADER`, written once at
    install time, and gets `native_session` built for the bearer's user — if its
    runtime's `native_gateway` flag allows one. Identity is asserted by config
    and credential either way, never chosen by the model, so a call carrying
    neither header is refused outright rather than guessed at."""

    async def resolve() -> GatewaySession:
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
                "bearer names none. Give this human their own under "
                "`users.<name>.secret`, run `octomate configure --secret` with "
                f"it on their machine, and re-run `octomate {agent} mcp "
                "install`."
            )
        return await native_session(octomate, client, principal)

    return resolve


async def native_session(
    octomate: Octomate, client: str, username: str
) -> GatewaySession:
    """An ephemeral gateway for one native call, never registered.

    `username` is the verified bearer's owner, so the session speaks for that
    person: anchored on a transient profile of theirs, its destinations are
    their own linked accounts. The call is still attributed to a runtime, never
    to a terminal session, so the session has no thread and no address — every
    destination is a crossing. Availability is the runtime's own
    `native_gateway` flag, and that refusal is the only real control: a static
    MCP config puts the tools in every session once installed.
    """
    config = octomate.config
    native_config = (
        {
            CLAUDE_NATIVE_ID: config.agents.claude,
            CODEX_NATIVE_ID: config.agents.codex,
            DEEPSEEK_NATIVE_ID: config.agents.deepseek,
        }[client]
        if config is not None
        else None
    )
    if native_config is None or not native_config.native_gateway:
        agent = client.removesuffix("-native")
        raise ToolError(
            f"The gateway is not offered to {client} sessions: `agents.{agent}` "
            f"is not configured here, or its `native_gateway` is off."
        )
    profile = await octomate.users.native_profile(client, username)
    if profile is None:
        raise RuntimeError(
            f"the bearer verified as {username!r} but the registry holds no such "
            "user — reconciliation runs before serving, so this is a wiring bug"
        )
    return GatewaySession(
        channel_routes=octomate.gateway.available_routes(
            octomate.channels, octomate.agents
        ),
        current_agent_id=client,
        channels=octomate.channels,
        users=octomate.users,
        user_profile=profile,
        agents=octomate.agents,
        native=True,
    )


def gateway_mcp(
    current: GatewaySession,
    thread_manager: ThreadManager,
    kick: Callable[[GatewayHandoffSignal], None] | None = None,
) -> FastMCP:
    """The gateway's spells as an MCP server.

    `current` is the FastMCP dependency each call resolves its session through —
    `Depends(...)` of a fixed session for a server mounted in-process for one turn,
    of a per-request lookup for a server that answers over HTTP. `thread_manager`
    is the ledger a delivering spell writes through. `kick` is how a native
    session's summon or scheme becomes its own turn at once, so only the served
    mount — the one place a native session can arrive — needs one.
    """
    mcp = FastMCP(name=GATEWAY_SERVER_NAME, instructions=GATEWAY_SERVER_INSTRUCTIONS)

    @mcp.tool(
        name=SCRY_TOOL_NAME, description=capability_contract(GatewayCapability.scry)
    )
    @spoken
    async def scry(session: GatewaySession = current) -> str:
        return str(await session.scry())

    @mcp.tool(
        name=SUMMON_TOOL_NAME, description=capability_contract(GatewayCapability.summon)
    )
    @spoken
    async def summon(
        agent_id: str,
        model: str,
        destination: SummonTarget,
        hint: str,
        reason: str,
        summon: str,
        effort: ThinkingEffort | None = None,
        session: GatewaySession = current,
    ) -> str:
        sentence = await session.summon(
            agent_id=agent_id,
            model=model,
            destination=destination,
            hint=hint,
            reason=reason,
            summon=summon,
            effort=effort,
        )
        if session.native:
            if kick is None:
                raise RuntimeError(
                    "a native session reached a gateway mounted without a kick"
                )
            kick(session.native_handoff())
        return sentence

    @mcp.tool(
        name=TELEPORT_TOOL_NAME,
        description=capability_contract(GatewayCapability.teleport),
    )
    @spoken
    async def teleport(
        hint: str,
        destination: TeleportTarget = THREAD_TARGET,
        session: GatewaySession = current,
    ) -> str:
        await session.teleport(hint=hint, destination=destination)
        return TELEPORT_RECORDED

    @mcp.tool(
        name=SCHEME_TOOL_NAME, description=capability_contract(GatewayCapability.scheme)
    )
    @spoken
    async def scheme(
        hint: str,
        brief: str,
        destination: SchemeTarget = DIRECT_TARGET,
        session: GatewaySession = current,
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
        name=SEND_TOOL_NAME, description=capability_contract(GatewayCapability.send)
    )
    @spoken
    async def send(
        segments: list[MessageSegment],
        destination: SendTarget = HERE_TARGET,
        session: GatewaySession = current,
    ) -> str:
        # Delivered right here: a gateway `send` has no run stream to ride, so the
        # delivery React performs for Inkling's sends happens in the call instead —
        # the same resolve, the same DM open, the same ledger row.
        address = await session.resolve_send(destination)
        target = address if address is not None else session.conversation_address
        if target is None:
            raise GatewayRefusal(
                "This session has no conversation of its own to land a send on — "
                f"name a destination from `{SCRY_TOOL_NAME}`."
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

    return mcp
