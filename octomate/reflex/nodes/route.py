"""Select the caller's agent, the thread's owner, or the channel's entry agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from pydantic_graph import BaseNode, GraphRunContext

from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    PendingHandoff,
    ReflexDeps,
    ReflexGraphResult,
    ReflexState,
)
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Route(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """Choose an agent for an inbound message, then enter `React`."""

    agent_id: str | None = (
        None  # Explicit caller choice takes precedence over ownership.
    )

    @reflex_logfire.instrument("reflex.route", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React:
        state = ctx.state
        source_target = state.source_target
        if source_target is None or source_target.address is None:
            raise ValueError("Route requires a resolved source target")

        source_address = source_target.address
        # Who owns the chat, not who owns this kick: a chat room's sub-thread is
        # opened fresh every time, and the owner a handoff pinned is the chat's.
        chat = (
            await ctx.deps.thread_manager.surface(state.thread)
            if self.agent_id is None and state.thread is not None
            else None
        )
        active_agent_id = chat.active_agent_tentacle_id if chat is not None else None
        agent_id = self.agent_id
        model = None
        hint = ""
        state.handoff = None
        if agent_id is not None:
            reason = "The caller selected this agent for the turn."
        elif chat is not None and active_agent_id is not None:
            agent_id = active_agent_id
            model = chat.active_model
            reason = hint = "Continuing with the active thread owner."
        elif (
            source_address.channel_thread_id
            and source_target.thread_strategy == "flat_thread"
        ):
            reflex_logfire.info(
                "route: flat-thread, skipping triage",
                channel_id=source_address.channel_tentacle_id,
                conversation_address=str(source_address),
            )
            reason = hint = "Continuing in the current thread."
            state.handoff = PendingHandoff()
        else:
            reason = "Entry agent."

        resolved = ctx.deps.resolve_agent(
            source_address.channel_tentacle_id, agent_id, model
        )
        await ctx.deps.load_pending_prompt(state, resolved.agent)
        state.decision = SummonDecision(
            action="summon",
            agent_id=resolved.agent,
            model=resolved.model,
            reason=reason,
            hint=hint,
            brief="",
        )
        state.target = replace(
            source_target, mode="sub" if source_address.channel_thread_id else "main"
        )
        return React()
