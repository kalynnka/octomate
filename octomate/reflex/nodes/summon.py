"""The node that performs a summon: lands the conversation where the decision
said, then re-enters `React` there."""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.crossing import open_crossing
from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire


@dataclass
class Summon(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """Performs a summon: opens the sub-thread or crossing the decision names, or
    takes the surface over in place, then re-enters `React` there."""

    # Refuse a failed thread open instead of continuing on the source surface.
    require_new_thread: bool = False

    @reflex_logfire.instrument("reflex.summon", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React | End[ReflexGraphResult]:
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
        hint_text = (
            decision.hint
            or decision.reason
            or "Octomate is continuing this request here."
        )

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

        if decision.new_thread:
            destination = decision.destination or target_address
            opened = await open_crossing(
                ctx, destination, source_address, hint_text, decision.agent_id
            )
            if opened is None:
                if self.require_new_thread:
                    raise ValueError("The destination could not create a thread.")
                return End(ReflexResult(decision=None, target=source_target))
            channel = ctx.deps.channel(opened.channel_tentacle_id)
            target = ResponseTarget(
                channel_id=channel.id,
                address=opened,
                thread_strategy=channel.thread_strategy,
                mode="sub",
            )
            state.moved_by = "summon"
        elif (
            decision.destination is not None and decision.destination != target_address
        ):
            raise ValueError(
                "Only the current conversation can be taken over in place."
            )

        state.target = target
        if target.address is not None and (
            state.thread is not None or decision.new_thread
        ):
            state.thread = await ctx.deps.thread_manager.enter(
                target.address, current=state.thread
            )
        return React()
