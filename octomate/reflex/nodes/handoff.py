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
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Handoff(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    @reflex_logfire.instrument("reflex.handoff", extract_args=False)
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
            raise ValueError("Handoff requires a decision and source target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("Handoff requires a summon decision")
        source_address = source_target.address
        channel = ctx.deps.channel(target)

        target_address = target.address
        if target_address is None:
            target_address = ChannelAddress(
                channel_tentacle_id=target.channel_id,
                chat_type=source_address.chat_type,
                chat_id=source_address.chat_id,
                user_id=source_address.user_id,
                thread_id="",
            )
            target = replace(target, address=target_address)

        if decision.destination == "here":
            # Take over the current conversation in place — no new surface. The
            # allow_here gate already refused this on a group main (Case 1).
            target = replace(target, address=target_address)
        elif not channel.surfaces.sub_thread:
            # Nothing to open on this platform — the handoff lands in the main chat.
            target = replace(target, mode="main")
        elif not target_address.thread_id:
            try:
                target_address = await channel.start_sub_thread(
                    target_address,
                    decision.hint
                    or decision.reason
                    or "Octomate is continuing this request here.",
                )
                target = replace(target, address=target_address)
            except Exception:
                logger.warning(
                    "Channel %s failed to start a sub-thread; using main target",
                    target.channel_id,
                    exc_info=True,
                )
                target = replace(target, mode="main")
        else:
            target = replace(target, address=target_address)

        state.target = target
        if target.address is not None and state.thread is not None:
            state.thread = await ctx.deps.thread_manager.ensure(target.address)
        return React()
