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
        state.thread = await ctx.deps.thread_manager.ensure(batch.target_address)
        state.user_prompt = None
        state.run_name = "resume"
        reflex_logfire.info("resume routes to React", batch_id=str(batch.id))
        return React(resume_batch_id=batch.id)
