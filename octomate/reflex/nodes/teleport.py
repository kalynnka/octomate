from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from pydantic_ai.tools import DeferredToolResults
from pydantic_graph import BaseNode, GraphRunContext

from octomate.reflex.crossing import open_crossing
from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    PendingHandoff,
    ReflexDeps,
    ReflexGraphResult,
    ReflexState,
    ResponseTarget,
)
from octomate.reflex.suspender import TeleportRequest
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Teleport(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """A `teleport` deferred call: fork the running agent's history into a fresh
    sub-thread and resume it there — of the current chat, or of this person's direct
    messages on another channel when the gate resolved one. The gate refuses the call
    outright where no sub-thread can be opened, so what is left here is the open
    that fails at the moment of asking — then resolve in place and stay put."""

    request: TeleportRequest
    origin: ResponseTarget
    agent_id: str

    @reflex_logfire.instrument("reflex.teleport", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React:
        state = ctx.state
        state.run_name = "teleport"
        origin = self.origin
        if origin.address is None or state.thread is None:
            raise ValueError("Teleport requires a resolved origin and thread")
        origin_address = origin.address
        hint = self.request.hint or "Octomate is continuing this request here."

        new_target = origin
        crossing = self.request.crossing
        if crossing is not None:
            crossed = await open_crossing(ctx, crossing, origin_address, hint)
            if crossed is not None:
                far = ctx.deps.channel(crossed.channel_tentacle_id)
                new_target = ResponseTarget(
                    channel_id=crossed.channel_tentacle_id,
                    address=crossed,
                    thread_strategy=far.thread_strategy,
                    mode="sub",
                )
        else:
            channel = ctx.deps.channel(origin)
            if channel.surfaces.sub_thread and not origin_address.channel_thread_id:
                try:
                    new_address = await channel.start_sub_thread(origin_address, hint)
                    new_target = replace(origin, address=new_address, mode="sub")
                except Exception:
                    logger.warning(
                        "Channel %s failed to open a teleport sub-thread; staying put",
                        origin.channel_id,
                        exc_info=True,
                    )

        if self.request.tool_call_id is not None:
            # An Inkling deferral: the pending call resolves into the resumed run.
            next = React(
                teleport_results=DeferredToolResults(
                    calls={
                        self.request.tool_call_id: "Continuing the conversation here."
                    }
                )
            )
        else:
            # A decision-reported teleport (a runtime that cannot be suspended
            # mid-run) left no pending call; the resumed run opens from the hint.
            next = React(continuation_prompt=hint)

        new_address = new_target.address
        if new_address is None or new_address == origin_address:
            # Stay put: the current conversation already holds the trailing teleport
            # deferral, so just resolve it and resume in place — nothing to fork.
            state.target = origin
            state.handoff = None
            return next

        # Move: fork the origin conversation into the new sub-thread, claim it for the
        # same agent so follow-ups continue there, and resume against the fork. The
        # resumable handle moves with it, so an external runtime's session continues
        # in the new place rather than beside it.
        new_thread = await ctx.deps.thread_manager.ensure(new_address)
        source_conversation = await ctx.deps.conversation_manager.ensure(
            state.thread.id, agent_tentacle_id=self.agent_id
        )
        target_conversation = await ctx.deps.conversation_manager.ensure(
            new_thread.id, agent_tentacle_id=self.agent_id
        )
        await ctx.deps.conversation_manager.fork(
            source_conversation, target_conversation, carry_external_id=True
        )
        state.thread = new_thread
        state.target = new_target
        state.handoff = PendingHandoff(source_agent_tentacle_id=self.agent_id)
        return next
