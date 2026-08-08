from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from pydantic_graph import BaseNode, GraphRunContext

from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexState,
)
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Route(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
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

        thread = state.thread
        active_agent_id = (
            thread.active_agent_tentacle_id if thread is not None else None
        )
        if thread is not None and active_agent_id is not None:
            active_model = thread.active_model
            ctx.deps.agent(active_agent_id)
            resolved = ctx.deps.resolve_agent(
                source_address.channel_tentacle_id,
                active_agent_id,
                active_model,
            )
            await ctx.deps.load_pending_prompt(state, resolved.agent)
            model = resolved.model
            state.decision = SummonDecision(
                action="summon",
                agent_id=resolved.agent,
                model=model,
                reason="Continuing with the active thread owner.",
                hint="Continuing with the active thread owner.",
                summon=str(state.user_prompt or ""),
            )
            state.target = replace(
                source_target,
                mode="sub" if source_address.channel_thread_id else "main",
            )
            state.claim_handoff = False
            state.handoff_from_agent_tentacle_id = None
            return React()

        if (
            source_address.channel_thread_id
            and source_target.thread_strategy == "flat_thread"
        ):
            reflex_logfire.info(
                "route: flat-thread, skipping triage",
                channel_id=source_address.channel_tentacle_id,
                conversation_address=str(source_address),
            )
            resolved = ctx.deps.resolve_agent(
                source_address.channel_tentacle_id,
                None,
                None,
            )
            await ctx.deps.load_pending_prompt(state, resolved.agent)
            model = resolved.model
            state.decision = SummonDecision(
                action="summon",
                agent_id=resolved.agent,
                model=model,
                reason="Continuing in the current thread.",
                hint="Continuing in the current thread.",
                summon=str(state.user_prompt or ""),
            )
            state.target = replace(source_target, mode="sub")
            state.claim_handoff = True
            state.handoff_from_agent_tentacle_id = None
            return React()

        # No active owner and not already in a flat thread: run the channel's default
        # agent directly in this conversation — no separate screening pass. It
        # self-routes via the gate toolset (summon / teleport) if it wants to hand off
        # or relocate. No handoff is recorded, so a group main is never pinned to an
        # owner (Case 1).
        resolved = ctx.deps.resolve_agent(
            source_address.channel_tentacle_id, None, None
        )
        await ctx.deps.load_pending_prompt(state, resolved.agent)
        state.decision = SummonDecision(
            action="summon",
            agent_id=resolved.agent,
            model=resolved.model,
            reason="Entry agent.",
            hint="",
            summon="",
        )
        state.target = replace(
            source_target, mode="sub" if source_address.channel_thread_id else "main"
        )
        state.claim_handoff = False
        state.handoff_from_agent_tentacle_id = None
        return React()
