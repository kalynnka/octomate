"""The node that runs the agent: records the handoff, drives the run onto the
channel's timeline, and acts on whatever decision or deferral the run left."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

from pydantic import UUID7
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexState,
)
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire
from octomate.tentacles.channel import ChannelOutput


@dataclass
class React(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """Runs the summoned agent on the target channel and acts on how the run ends:
    a reply, a spell to perform, or a deferral to park."""

    resume_batch_id: UUID7 | None = None
    # Set by Teleport to resume the same agent where it landed, with its pending
    # call resolved — against the forked history, or in place.
    resume_results: DeferredToolResults | None = None

    @reflex_logfire.instrument("reflex.react", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React | Summon | Scheme | Teleport | End[ReflexGraphResult]:
        """React, and leave the turn's workspace in the mirror however it ends.

        In a `finally` because a turn that raised still did whatever it did on
        disk. The files an agent wrote before its provider dropped the connection
        are work, and until this runs the workspace is the only copy of them —
        which also means the sweep can never reclaim that workspace, since it
        refuses anything the mirror has not seen. A failed turn on a thread nobody
        resumes would otherwise hold its disk for good.

        A cancelled turn is the one case this does not cover: the save's first
        await raises straight back out, so its work stays in the workspace and the
        sweep keeps it, which is the safe direction.
        """
        try:
            return await self.react(ctx)
        finally:
            if ctx.state.thread is not None:
                await ctx.deps.workspaces.save(ctx.state.thread)

    async def react(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React | Summon | Scheme | Teleport | End[ReflexGraphResult]:
        state = ctx.state
        decision = state.decision
        target = state.target
        source_target = state.source_target
        if (
            decision is None
            or target is None
            or target.address is None
            or source_target is None
            or source_target.address is None
        ):
            raise ValueError("React requires a decision and resolved target")
        if not isinstance(decision, SummonDecision):
            raise ValueError("React requires a summon decision")
        target_address = target.address
        # Against the channel the run will happen on, not the one it came from. A
        # spell that moves the turn to another channel leaves those two different,
        # and which agents serve a channel is that channel's own config — resolving
        # against the origin swaps in whatever the origin happens to list first.
        resolved = ctx.deps.resolve_agent(
            target.channel_id, decision.agent_id, decision.model
        )
        model = resolved.model
        decision = decision.model_copy(
            update={"agent_id": resolved.agent, "model": model}
        )
        state.decision = decision
        state.selection = decision.key
        state.conversation_id = None
        agent = ctx.deps.agent(resolved.agent)
        run_model = agent.models[model] if model is not None else None
        thread_id = state.thread.id if state.thread else None
        claim = state.handoff
        if state.thread is not None and claim is not None:
            await claim.land(ctx.deps, state.thread, decision)
            state.handoff = None
        runtime = ctx.deps.runtime
        session, suspender, capabilities = await runtime.resources(ctx)
        # A summon names the level it hands over at; every other turn runs at the
        # one the conversation was set to.
        effort = decision.effort
        if effort is None and state.conversation_id is not None:
            conversation = await ctx.deps.conversation_manager.get(
                state.conversation_id, with_history=False
            )
            effort = await agent.resolve_effort(conversation, model=model)
        if state.user_profile is not None:
            await runtime.prepare_user(
                ctx,
                state.user_profile,
                session=session,
                suspender=suspender,
                capabilities=capabilities,
            )
        deferred_results = self.resume_results
        if self.resume_batch_id is not None:
            batch = await ctx.deps.action_manager.get_batch(self.resume_batch_id)
            deferred_results = batch.build_results()
        user_prompt: str | Sequence[UserContent] | None = (
            None
            if deferred_results is not None
            else decision.summon or str(state.user_prompt or "")
        )

        async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
            async with agent.run_stream_events(
                user_prompt,
                conversation_address=target_address,
                thread_id=thread_id,
                source_thread_address=state.source_thread_address,
                source_thread_message_ids=state.source_thread_message_ids,
                run_name=state.run_name,
                model=run_model,
                effort=effort,
                deferred_tool_results=deferred_results,
                deferred_suspender=suspender,
                capabilities=capabilities,
            ) as stream:
                async for event in stream:
                    yield event

        with reflex_logfire.span(
            "react",
            channel_id=target_address.channel_tentacle_id,
            agent_id=agent.id,
            conversation_address=str(target_address),
            streaming=suspender.channel.config.stream.enabled,
            resume_batch_id=str(self.resume_batch_id) if self.resume_batch_id else None,
        ) as span:
            async with ctx.deps.gateway.driving(
                session, conversation_id=state.conversation_id
            ):
                if suspender.channel.config.stream.enabled:
                    result = await runtime.drive(ctx, suspender, events())
                else:
                    result = await agent.run(
                        user_prompt,
                        conversation_address=target_address,
                        thread_id=thread_id,
                        source_thread_address=state.source_thread_address,
                        source_thread_message_ids=state.source_thread_message_ids,
                        run_name=state.run_name,
                        model=run_model,
                        effort=effort,
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                        capabilities=capabilities,
                    )
                span.set_attribute("react.run_id", result.run_id)
                span.set_attribute(
                    "react.deferred", isinstance(result.output, DeferredToolRequests)
                )
                await runtime.present_result(
                    ctx,
                    suspender,
                    result,
                    streamed=suspender.channel.config.stream.enabled,
                )
                if self.resume_batch_id is not None:
                    await ctx.deps.action_manager.mark_batch(
                        self.resume_batch_id, "completed", completed=True
                    )
                return await runtime.finish(ctx, session, suspender, result)


# React and the three nodes it hands off to name each other in their `run` return
# hints, and pydantic-graph resolves those hints against this module's globals when
# the graph is built — so `if TYPE_CHECKING` is not enough, the names must really be
# here. Importing them at the top would deadlock the cycle (react would be half-built
# when summon asked for it), so the cycle is closed here instead, after `React`
# exists. `nodes/__init__` imports this module first to keep that order.
from octomate.reflex.nodes.scheme import Scheme  # noqa: E402
from octomate.reflex.nodes.summon import Summon  # noqa: E402
from octomate.reflex.nodes.teleport import Teleport  # noqa: E402
