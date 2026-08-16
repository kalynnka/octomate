from __future__ import annotations

import logging
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
from octomate.schemas.triage import CrossingLanding, HereLanding, SummonDecision
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Handoff(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    @reflex_logfire.instrument("reflex.handoff", extract_args=False)
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
            raise ValueError("Handoff requires a decision and source target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("Handoff requires a summon decision")
        source_address = source_target.address
        channel = ctx.deps.channel(target)
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

        if isinstance(decision.destination, CrossingLanding):
            crossed = await open_crossing(
                ctx, decision.destination, source_address, hint_text
            )
            if crossed is None:
                return End(ReflexResult(decision=None, target=source_target))
            far = ctx.deps.channel(crossed.channel_tentacle_id)
            state.target = ResponseTarget(
                channel_id=crossed.channel_tentacle_id,
                address=crossed,
                thread_strategy=far.thread_strategy,
                mode="sub",
            )
            state.thread = await ctx.deps.thread_manager.ensure(crossed)
            return React()

        if isinstance(decision.destination, HereLanding):
            # Take over the current conversation in place — no new surface. The
            # allow_here gate already refused this on a group main (Case 1).
            target = replace(target, address=target_address)
        else:
            # The gate refused this destination unless a sub-thread can be opened
            # from here, so there is one place to try and no fallback to pick.
            try:
                opened = await channel.start_sub_thread(target_address, hint_text)
            except Exception:
                logger.warning(
                    "Channel %s raised starting a sub-thread",
                    target.channel_id,
                    exc_info=True,
                )
                opened = target_address
            group_main = target_address.shared and not target_address.channel_thread_id
            if opened == target_address and group_main:
                # Nothing moved, and the surface it would fall back to is a group's
                # main channel. An ink swallows its own send failures, so a failed
                # open hands back the address it was given rather than raising —
                # and handing over on that one pins an owner where `allow_here`
                # refuses to, which then answers every later message from anyone.
                # Leave the turn with the agent that already replied.
                logger.warning(
                    "Channel %s opened no sub-thread for the summon to %s; "
                    "leaving the turn here rather than claiming a group main",
                    target.channel_id,
                    decision.agent_id,
                )
                return End(ReflexResult(decision=None, target=source_target))
            # Either it opened, or it did not and this surface can be taken over in
            # place — which is a destination the gate would have allowed from here.
            target = replace(target, address=opened)

        state.target = target
        if target.address is not None and state.thread is not None:
            state.thread = await ctx.deps.thread_manager.ensure(target.address)
        return React()
