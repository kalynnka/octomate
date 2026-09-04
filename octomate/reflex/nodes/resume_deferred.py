from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic_ai import AgentRunResult
from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    DeferredResult,
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.triage import SummonDecision
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class ResumeDeferred(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    awake: DeferredActionBatchResponse

    @reflex_logfire.instrument("reflex.resume_deferred", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React | End[ReflexGraphResult]:
        state = ctx.state
        with reflex_logfire.span(
            "resume_deferred",
            batch_id=str(self.awake.batch_id),
        ) as span:
            batch = await ctx.deps.action_manager.resolve_batch(self.awake)
            span.set_attribute("run_name", batch.run_name)
            span.set_attribute("batch_status", batch.status)
            span.set_attribute("completed", batch.completed)
        target_channel = ctx.deps.channels.get(batch.target_address.channel_tentacle_id)
        if target_channel is None:
            raise ValueError(
                f"unknown channel {batch.target_address.channel_tentacle_id!r}"
            )
        ctx.deps.agent(batch.agent_tentacle_id)
        target = ResponseTarget(
            channel_id=batch.target_address.channel_tentacle_id,
            address=batch.target_address,
            thread_strategy=target_channel.thread_strategy,
            mode=batch.target_mode,
        )
        source_channel = ctx.deps.channels.get(batch.source_address.channel_tentacle_id)
        state.target = target
        state.source_target = ResponseTarget(
            channel_id=batch.source_address.channel_tentacle_id,
            address=batch.source_address,
            thread_strategy=(
                source_channel.thread_strategy
                if source_channel is not None
                else target_channel.thread_strategy
            ),
            mode="main",
        )

        if isinstance(batch.decision, SummonDecision):
            decision = batch.decision
        elif batch.decision:
            decision = SummonDecision.model_validate(batch.decision)
        else:
            resolved = ctx.deps.resolve_agent(
                batch.source_address.channel_tentacle_id,
                batch.agent_tentacle_id,
                None,
            )
            model = resolved.model
            decision = SummonDecision(
                action="summon",
                agent_id=resolved.agent,
                model=model,
                reason="Resuming deferred human input.",
                hint="Resuming deferred human input.",
                summon="",
            )
        if not isinstance(decision, SummonDecision):
            raise ValueError("react deferred batch requires a summon decision")
        decision = decision.model_copy(update={"agent_id": batch.agent_tentacle_id})
        state.decision = decision
        if batch.status in {"completed", "resuming"}:
            reflex_logfire.info("resume already completed", batch_id=str(batch.id))
            return End(ReflexResult(decision=decision, target=target))
        if not batch.completed:
            return End(
                DeferredResult(
                    requests=batch.requests,
                    target=target,
                    run_name=batch.run_name or "react",
                    result=AgentRunResult(batch.requests),
                    batch_id=batch.id,
                )
            )

        await ctx.deps.action_manager.mark_batch(batch.id, "resuming")
        # The resumed agent run continues the batch's conversation, so bind the
        # thread that owns it (React reads state.thread for the run's thread_id).
        # Through the conversation and not the address: a chat room's kick ran in a
        # sub-thread, and the address names the chat it was seen on, which would
        # answer the deferral in a context the suspended run never had.
        thread_id = await ctx.deps.conversation_manager.thread_id(batch.conversation_id)
        if thread_id is None:
            raise ValueError(
                f"deferred batch {batch.id} names conversation "
                f"{batch.conversation_id}, which does not exist"
            )
        thread = await ctx.deps.thread_manager.get(thread_id, with_messages=False)
        if thread is None:
            raise ValueError(
                f"conversation {batch.conversation_id} names thread {thread_id}, "
                "which does not exist"
            )
        state.thread = thread
        # The resumed run must serve the user the suspended run served: React
        # mounts their per-user capabilities (and the gate's identity) from
        # state.user_profile, and without it the run loses tools mid-conversation
        # — which also breaks the provider prompt cache both ways.
        state.user_profile = await ctx.deps.thread_manager.users.profile(
            batch.source_address.channel_tentacle_id,
            batch.source_address.user_id,
        )
        state.user_prompt = None
        state.run_name = "resume"
        reflex_logfire.info("resume routes to React", batch_id=str(batch.id))
        return React(resume_batch_id=batch.id)
