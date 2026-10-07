"""The node that performs a `teleport`: carries the running agent's history
somewhere else and resumes it there."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai.tools import DeferredToolResults
from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.crossing import open_crossing
from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    PendingHandoff,
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.reflex.suspender import TeleportRequest
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
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
    ) -> React | End[ReflexGraphResult]:
        state = ctx.state
        state.run_name = "teleport"
        origin = self.origin
        if origin.address is None or state.thread is None:
            raise ValueError("Teleport requires a resolved origin and thread")
        origin_address = origin.address
        source = state.thread
        hint = self.request.hint or "Octomate is continuing this request here."
        sentence = "Continuing the conversation here."

        new_target = origin
        if self.request.new_thread:
            opened = await open_crossing(
                ctx, self.request.destination, origin_address, hint, self.agent_id
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
                if self.request.prompt is None and not self.request.resume:
                    return await self.land(ctx)
                return await self.carry_on(ctx, sentence)
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
            state.handoff = PendingHandoff(
                source_agent_tentacle_id=self.agent_id,
                source_conversation_id=source_conversation.id,
            )

        project = self.request.project
        carried = (
            await ctx.deps.workspaces.projects.of(source)
            if project is None and landed.id != source.id
            else None
        )
        if project is not None:
            state.thread = await self.bind(ctx, landed, project)
            sentence = (
                f"Continuing the conversation here, in the workspace of {project!r}."
            )
        elif carried is not None:
            # The conversation was about a project, and its history is of work in
            # that tree: the thread it lands in is about the same one, as it stands.
            state.thread = await ctx.deps.thread_manager.bind(landed.id, carried)
            await ctx.deps.workspaces.carry(source, state.thread)
            sentence = (
                "Continuing the conversation here, in a workspace of "
                f"{carried.name!r} that holds your work as you left it."
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
            state.thread = await ctx.deps.thread_manager.rename(
                state.thread, f"Fork of {source.title or 'conversation'}"
            )
        elif not self.request.new_thread:
            external_id = await agent.fork_session(conversation, cwd=cwd)
            if external_id is not None and external_id != conversation.external_id:
                await ctx.deps.conversation_manager.set_external_id(
                    conversation, external_id
                )
            await agent.relocate(conversation, cwd=cwd)
        if self.request.prompt is None and not self.request.resume:
            return await self.land(ctx)
        return await self.carry_on(ctx, sentence)

    async def carry_on(
        self, ctx: GraphRunContext[ReflexState, ReflexDeps], sentence: str
    ) -> React:
        """Run the agent where the move landed: on the user's prompt, recorded there
        as their message, or on its own call answered by where it now is."""
        state = ctx.state
        address = state.target.address if state.target is not None else None
        if address is None:
            raise ValueError("Teleport carries on only where the move landed")
        prompt = self.request.prompt
        if prompt is None:
            # The pending call resolves into the resumed run, whichever runtime cast it.
            if self.request.tool_call_id is None:
                state.user_prompt = f"{sentence}\nCurrent channel address: {address}"
                return React()
            return React(
                resume_results=DeferredToolResults(
                    calls={self.request.tool_call_id: sentence}
                )
            )
        if state.user_profile is None:
            raise ValueError("A prompt after a teleport needs its user")
        channel = ctx.deps.channel(address.channel_tentacle_id)
        message = await ctx.deps.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=channel.id,
                chat_type=address.chat_type,
                chat_id=address.chat_id,
                user_id=address.user_id,
                channel_thread_id=address.channel_thread_id,
                shared=address.shared,
                self_id=channel.self_profile.channel_user_id,
                sender=state.user_profile,
                segments=[TextSegment(data={"text": prompt})],
                raw=prompt,
            )
        )
        state.source_target = state.target
        state.trigger_thread_message_id = message.id
        await ctx.deps.load_pending_prompt(state, self.agent_id)
        return React()

    async def land(
        self, ctx: GraphRunContext[ReflexState, ReflexDeps]
    ) -> End[ReflexGraphResult]:
        """End the move where it landed, for the next message there to carry on:
        the handoff is recorded and the workspace saved, as a turn's end would."""
        state = ctx.state
        if state.decision is None or state.target is None or state.thread is None:
            raise ValueError("Teleport lands with a decision, a target and a thread")
        resolved = ctx.deps.resolve_agent(
            state.target.channel_id, state.decision.agent_id, state.decision.model
        )
        state.decision = state.decision.model_copy(
            update={"agent_id": resolved.agent, "model": resolved.model}
        )
        if state.handoff is not None:
            await state.handoff.land(ctx.deps, state.thread, state.decision)
            state.handoff = None
        await ctx.deps.workspaces.save(state.thread)
        return End(ReflexResult(decision=state.decision, target=state.target))

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
