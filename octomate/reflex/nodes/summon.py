"""The node that performs a summon: hands the conversation, where it is, to the agent
the decision names, then re-enters `React` there."""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic_graph import BaseNode, GraphRunContext

from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexState,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire


@dataclass
class Summon(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """Performs a summon: takes the surface over in place for the agent the decision
    names, then re-enters `React` there."""

    @reflex_logfire.instrument("reflex.summon", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React:
        state = ctx.state
        decision = state.decision
        target = state.target
        source_target = state.source_target
        if (
            decision is None
            or target is None
            or source_target is None
            or source_target.address is None
        ):
            raise ValueError("Summon requires a decision and source target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("Summon requires a summon decision")
        source_address = source_target.address
        target_address = target.address
        if target_address is None:
            target_address = ChannelAddress(
                channel_tentacle_id=target.channel_id,
                chat_type=source_address.chat_type,
                chat_id=source_address.chat_id,
                user_id=source_address.user_id,
                channel_thread_id=None,
                shared=source_address.shared,
            )
            target = replace(target, address=target_address)

        state.target = target
        if state.thread is not None:
            state.thread = await ctx.deps.thread_manager.enter(
                target_address, current=state.thread
            )
        return React()
