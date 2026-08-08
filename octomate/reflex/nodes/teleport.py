from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from pydantic_ai.tools import DeferredToolResults
from pydantic_graph import BaseNode, GraphRunContext

from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
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
    sub-thread of the current chat and resume it there. When a new thread can't be
    opened (main_only, or already inside a thread), resolve in place and stay put."""

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

        channel = ctx.deps.channel(origin)
        new_target = origin
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

        results = DeferredToolResults(
            calls={self.request.tool_call_id: "Continuing the conversation here."}
        )

        new_address = new_target.address
        if new_address is None or new_address == origin_address:
            # Stay put: the current conversation already holds the trailing teleport
            # deferral, so just resolve it and resume in place — nothing to fork.
            state.target = origin
            state.claim_handoff = False
            return React(teleport_results=results)

        # Move: fork the origin conversation into the new sub-thread, claim it for the
        # same agent so follow-ups continue there, and resume against the fork.
        new_thread = await ctx.deps.thread_manager.ensure(new_address)
        source_conversation = await ctx.deps.conversation_manager.ensure(
            state.thread.id, agent_tentacle_id=self.agent_id
        )
        target_conversation = await ctx.deps.conversation_manager.ensure(
            new_thread.id, agent_tentacle_id=self.agent_id
        )
        await ctx.deps.conversation_manager.fork(
            source_conversation, target_conversation
        )
        state.thread = new_thread
        state.target = new_target
        state.claim_handoff = True
        state.handoff_from_agent_tentacle_id = self.agent_id
        return React(teleport_results=results)
