from __future__ import annotations

from dataclasses import dataclass

from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.nodes.resume_deferred import ResumeDeferred
from octomate.reflex.nodes.route import Route
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.schemas.awakes import AwakeSignal, DeferredActionBatchResponse
from octomate.telemetry import reflex_logfire


@dataclass
class Awake(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    signal: AwakeSignal

    @reflex_logfire.instrument("reflex.awake", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> Route | ResumeDeferred | End[ReflexGraphResult]:
        if isinstance(self.signal, DeferredActionBatchResponse):
            return ResumeDeferred(awake=self.signal)

        if not self.signal:
            reflex_logfire.info("awake short-circuit: empty signal")
            return End(
                ReflexResult(
                    decision=None,
                    target=ResponseTarget(channel_id=""),
                )
            )

        address = self.signal.address
        channel = ctx.deps.channels.get(address.channel_tentacle_id)
        if channel is None:
            raise ValueError(f"unknown channel {address.channel_tentacle_id!r}")

        source_target = ResponseTarget(
            channel_id=address.channel_tentacle_id,
            address=address,
            thread_strategy=channel.thread_strategy,
            mode="main",
        )
        ctx.state.source_target = source_target
        ctx.state.thread = await ctx.deps.thread_manager.ensure(address)
        ctx.state.trigger_thread_message_id = self.signal.trigger_thread_message_id
        ctx.state.user_profile = self.signal.messages[-1].sender

        user_prompt = "\n\n".join(str(event) for event in self.signal.messages).strip()
        ctx.state.user_prompt = user_prompt
        if not user_prompt and self.signal.trigger_thread_message_id is None:
            reflex_logfire.info(
                "awake short-circuit: empty prompt",
                channel_id=address.channel_tentacle_id,
                conversation_address=str(address),
            )
            return End(
                ReflexResult(
                    decision=None,
                    target=source_target,
                )
            )
        return Route()
