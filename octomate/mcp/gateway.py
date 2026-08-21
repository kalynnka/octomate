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
from typing import ParamSpec, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from pydantic_ai.settings import ThinkingEffort

from octomate.capabilities.gateway import GatewayCapability, gateway_instructions
from octomate.managers.gateway import GatewayManager, GatewayRefusal, GatewaySession
from octomate.managers.thread import ThreadManager
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


def served_session(gateway: GatewayManager) -> Callable[[], GatewaySession]:
    """The session a call served over HTTP runs against: the turn registered at
    `gateway` under the conversation the request's `CONVERSATION_HEADER` names.
    Identity is asserted by the launch config, not chosen by the model, so a call
    without the header, or naming a conversation with no turn in flight, is
    refused outright rather than guessed at."""

    def resolve() -> GatewaySession:
        named = get_http_headers().get(CONVERSATION_HEADER.lower())
        if named is None:
            raise ToolError(
                f"This call names no conversation (no {CONVERSATION_HEADER} header); "
                "the gateway answers only a driven turn whose launch config names "
                "its own."
            )
        try:
            conversation_id = uuid.UUID(named)
        except ValueError:
            raise ToolError(
                f"{CONVERSATION_HEADER} is not a conversation id: {named!r}."
            ) from None
        session = gateway.get(conversation_id)
        if session is None:
            raise ToolError(
                f"No turn of conversation {named} is at the gateway; a session "
                "reaches it only while the run that opened it is in flight."
            )
        return session

    return resolve


def gateway_mcp(current: GatewaySession, thread_manager: ThreadManager) -> FastMCP:
    """The gateway's spells as an MCP server.

    `current` is the FastMCP dependency each call resolves its session through —
    `Depends(...)` of a fixed session for a server mounted in-process for one turn,
    of a per-request lookup for a server that answers over HTTP. `thread_manager`
    is the ledger a delivering spell writes through.
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
        return await session.summon(
            agent_id=agent_id,
            model=model,
            destination=destination,
            hint=hint,
            reason=reason,
            summon=summon,
            effort=effort,
        )

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
        return await session.scheme(hint=hint, brief=brief, destination=destination)

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
