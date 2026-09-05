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
from octomate.schemas.thread import Thread
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Teleport(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """A `teleport` deferred call: carry the running agent's history somewhere else
    and resume it there — a fresh sub-thread of the current chat, of this person's
    direct messages on another channel when the gate resolved a crossing, or this
    very thread when the move is only into a project's workspace. The gate refuses
    the call outright where no sub-thread can be opened, so what is left here is
    the open that fails at the moment of asking — then resolve in place and stay
    put. With a project, the thread landed in is bound to it and its workspace is
    forked, so the resumed run starts in the project's code."""

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
        if self.request.here:
            # Asked to stay: the move is into a project's workspace, and this
            # thread is what gets bound. Nothing to open.
            pass
        elif crossing is not None:
            crossed = await open_crossing(
                ctx, crossing, origin_address, hint, self.agent_id
            )
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
                    if new_address != origin_address:
                        await ctx.deps.record_move(
                            origin_address,
                            hint,
                            agent_tentacle_id=self.agent_id,
                            platform_message_id=new_address.channel_thread_id,
                        )
                    new_target = replace(origin, address=new_address, mode="sub")
                except Exception:
                    logger.warning(
                        "Channel %s failed to open a teleport sub-thread; staying put",
                        origin.channel_id,
                        exc_info=True,
                    )

        new_address = new_target.address
        if new_address is None or new_address == origin_address:
            # Stay put: the current conversation already holds the trailing teleport
            # deferral, so just resolve it and resume in place — nothing to fork.
            state.target = origin
            state.handoff = None
            landed = state.thread
            conversation = await ctx.deps.conversation_manager.ensure(
                landed.id, agent_tentacle_id=self.agent_id
            )
        else:
            # Move: fork the origin conversation into the new sub-thread, claim it
            # for the same agent so follow-ups continue there, and resume against
            # the fork. The resumable handle moves with it, so an external runtime's
            # session continues in the new place rather than beside it.
            landed = await ctx.deps.thread_manager.enter(
                new_address, current=state.thread
            )
            source_conversation = await ctx.deps.conversation_manager.ensure(
                state.thread.id, agent_tentacle_id=self.agent_id
            )
            target_conversation = await ctx.deps.conversation_manager.ensure(
                landed.id, agent_tentacle_id=self.agent_id
            )
            await ctx.deps.conversation_manager.fork(
                source_conversation, target_conversation, carry_external_id=True
            )
            conversation = target_conversation
            state.thread = landed
            state.target = new_target
            state.handoff = PendingHandoff(source_agent_tentacle_id=self.agent_id)

        sentence = "Continuing the conversation here."
        project = self.request.project
        if project is not None:
            state.thread = await self.bind(ctx, landed, project)
            sentence = (
                f"Continuing the conversation here, in the workspace of {project!r}."
            )
        # The agent resumes in another directory either way — a new thread's own
        # workspace, or the project's — and a runtime session may be filed under
        # the one it ran in. The tentacle knows how to move its own; this is when.
        cwd = ctx.deps.workspaces.open(
            state.thread.id, await ctx.deps.workspaces.projects.of(state.thread)
        ).path
        await ctx.deps.agent(self.agent_id).relocate(conversation, cwd=cwd)
        # The pending call resolves into the resumed run, whichever runtime cast it.
        return React(
            resume_results=DeferredToolResults(
                calls={self.request.tool_call_id: sentence}
            )
        )

    async def bind(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        thread: Thread,
        name: str,
    ) -> Thread:
        """Bind the thread landed in to the project the request names, and fork
        its workspace, so the resumed run starts in the project's code. The gate
        validated the project and the ref; a project missing here is a wiring
        bug. Answers the bound thread, re-read: the state's copy predates the
        binding, and the turn-end save reads the project off it."""
        project = ctx.deps.workspaces.projects.get(name)
        if project is None:
            raise RuntimeError(
                f"project {name!r} vanished between the gate and the move"
            )
        mirror = await ctx.deps.workspaces.mirrors.sync(project)
        bound = await ctx.deps.thread_manager.bind(thread.id, project)
        await ctx.deps.workspaces.materialize(
            ctx.deps.workspaces.open(thread.id, project), mirror, self.request.ref
        )
        return bound
