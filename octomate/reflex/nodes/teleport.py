"""The node that performs a `teleport`: carries the running agent's history
somewhere else and resumes it there."""

from __future__ import annotations

from dataclasses import dataclass

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
from octomate.schemas.thread import Thread, ThreadKey
from octomate.telemetry import reflex_logfire


@dataclass
class Teleport(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """Create a thread at the prepared address, copy history and resume the agent.

    An in-place move binds a project without creating a thread. A failed creation
    refuses the move before any history is copied.
    """

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
        if self.request.new_thread:
            destination = self.request.destination or origin_address
            opened = await open_crossing(
                ctx, destination, origin_address, hint, self.agent_id
            )
            if opened is not None:
                channel = ctx.deps.channel(opened.channel_tentacle_id)
                new_target = ResponseTarget(
                    channel_id=channel.id,
                    address=opened,
                    thread_strategy=channel.thread_strategy,
                    mode="sub",
                )

        new_address = new_target.address
        if self.request.new_thread and new_address == origin_address:
            raise ValueError(
                "The destination could not create a thread; nothing was teleported."
            )
        source_conversation = None
        if new_address is None or new_address == origin_address:
            # The current conversation already holds the trailing teleport
            # deferral. An explicit workspace move may still fork its runtime.
            state.target = origin
            state.handoff = None
            landed = state.thread
            conversation = await ctx.deps.conversation_manager.ensure(
                landed.id, agent_tentacle_id=self.agent_id
            )
        else:
            # Move: fork the origin conversation into the new sub-thread, claim it
            # for the same agent so follow-ups continue there, and resume against
            # the fork. The runtime prepares its destination handle after the
            # destination workspace is ready.
            if state.thread.kind == "native_thread":
                source_agent_id = state.thread.active_agent_tentacle_id
                if (
                    state.user_profile is None
                    or state.decision is None
                    or source_agent_id is None
                ):
                    raise ValueError(
                        "Native teleport requires its requesting user and agent."
                    )
                source = await ctx.deps.conversation_manager.ensure(
                    state.thread.id,
                    agent_tentacle_id=source_agent_id,
                )
                agent = ctx.deps.agent(self.agent_id)
                landed = await agent.fork(
                    source,
                    ThreadKey.from_address(new_address),
                    sender=state.user_profile,
                )
                copied = await ctx.deps.conversation_manager.ensure(
                    landed.id, agent_tentacle_id=agent.id
                )
                state.decision = state.decision.model_copy(
                    update={"model": copied.runs[-1].model_name}
                )
                state.thread = landed
                state.target = new_target
                state.handoff = None
                state.user_prompt = f"Continuing the conversation here.\nCurrent channel address: {new_address}"
                return React()
            landed = await ctx.deps.thread_manager.enter(
                new_address, current=state.thread
            )
            source_conversation = await ctx.deps.conversation_manager.ensure(
                state.thread.id, agent_tentacle_id=self.agent_id
            )
            target_conversation = await ctx.deps.conversation_manager.ensure(
                landed.id, agent_tentacle_id=self.agent_id
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
        agent = ctx.deps.agent(self.agent_id)
        if source_conversation is not None:
            external_id = await agent.fork_session(source_conversation, cwd=cwd)
            carry = (
                external_id is not None
                and external_id == source_conversation.external_id
            )
            await ctx.deps.conversation_manager.fork(
                source_conversation,
                conversation,
                carry_external_id=carry,
                external_id=None if carry else external_id,
                model_name=state.decision.model if state.decision is not None else None,
            )
            await agent.relocate(conversation, cwd=cwd)
        elif not self.request.new_thread:
            external_id = await agent.fork_session(conversation, cwd=cwd)
            if external_id is not None and external_id != conversation.external_id:
                await ctx.deps.conversation_manager.set_external_id(
                    conversation, external_id
                )
            await agent.relocate(conversation, cwd=cwd)
        # The pending call resolves into the resumed run, whichever runtime cast it.
        if self.request.tool_call_id is None:
            state.user_prompt = f"{sentence}\nCurrent channel address: {new_address}"
            return React()
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
