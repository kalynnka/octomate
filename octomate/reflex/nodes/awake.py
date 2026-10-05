"""The graph's entry node: reads the signal that woke it and picks the first
transition."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.nodes.resume_deferred import ResumeDeferred
from octomate.reflex.nodes.route import Route
from octomate.reflex.state import (
    PendingHandoff,
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.reflex.suspender import TeleportRequest
from octomate.schemas.awakes import (
    AwakeSignal,
    CommandSignal,
    DeferredActionBatchResponse,
    DrivenGatewaySignal,
    NativeGatewaySignal,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.schemas.triage import SummonDecision, TeleportDecision
from octomate.schemas.user import UserProfile
from octomate.telemetry import reflex_logfire


@dataclass
class Awake(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """The entry node: explicit commands dispatch, batch replies resume, native
    handoffs land, and user messages resolve their thread and go to `Route`."""

    signal: AwakeSignal

    @reflex_logfire.instrument("reflex.awake", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> (
        Command
        | Route
        | ResumeDeferred
        | Summon
        | Teleport
        | Scheme
        | End[ReflexGraphResult]
    ):
        if isinstance(self.signal, CommandSignal):
            return Command(signal=self.signal)

        if isinstance(self.signal, DeferredActionBatchResponse):
            return ResumeDeferred(awake=self.signal)

        if isinstance(self.signal, DrivenGatewaySignal):
            signal = self.signal
            ctx.state.operated_from = signal.operated_from
            return await self.thread_operation(
                ctx,
                signal.thread_id,
                signal.decision,
                agent_id=signal.agent_id,
                sender=signal.user_profile,
                source=signal.source,
                requested_from="Trunkline",
            )

        if isinstance(self.signal, NativeGatewaySignal):
            # A native session's spell, already validated at the gateway: enter
            # the graph where React's post-run consumption would have, with the
            # native pseudo-channel — which nobody serves, so no lookup — as the
            # source.
            source = self.signal.source
            if source is None:
                raise ValueError("a gateway handoff signal needs its source address")
            decision = self.signal.decision
            if isinstance(decision, TeleportDecision):
                if self.signal.user_profile is None:
                    raise ValueError("a native teleport needs its thread's owner")
                return await self.thread_operation(
                    ctx,
                    ThreadKey.from_address(source),
                    decision,
                    agent_id=self.signal.agent_id,
                    sender=self.signal.user_profile,
                    source=source,
                    requested_from=self.signal.agent_id,
                )
            source_target = ResponseTarget(
                channel_id=source.channel_tentacle_id, address=source
            )
            ctx.state.source_target = source_target
            ctx.state.user_profile = self.signal.user_profile
            ctx.state.handoff = PendingHandoff(
                source_agent_tentacle_id=self.signal.agent_id
            )
            return Scheme(
                request=decision,
                origin=source_target,
                agent_id=self.signal.agent_id,
            )

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
        ctx.state.thread = await ctx.deps.thread_manager.enter(address)
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

    async def thread_operation(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
        thread_ref: uuid.UUID | ThreadKey,
        decision: SummonDecision | TeleportDecision,
        *,
        agent_id: str,
        sender: UserProfile,
        source: ChannelAddress,
        requested_from: str,
    ) -> Summon | Teleport:
        """Enter an existing thread its owner acts on: the console's operation on
        any thread, or a native session's teleport of its own."""
        origin = ResponseTarget(
            channel_id=source.channel_tentacle_id,
            address=source,
            mode="sub" if source.channel_thread_id else "main",
        )
        # Set first, so a refusal below is still reported.
        ctx.state.source_target = origin
        thread = await ctx.deps.thread_manager.get(
            thread_ref, with_messages=False, user_id=sender.user_id
        )
        if thread is None or thread.active_agent_tentacle_id != agent_id:
            raise ValueError("The source thread changed; reload its operations.")
        ctx.state.thread = thread
        ctx.state.user_profile = sender
        ctx.state.target = origin
        if isinstance(decision, TeleportDecision):
            agent = ctx.deps.agent(decision.agent_id)
            conversation = next(
                c
                for c in thread.conversations
                if c.agent_tentacle_id == agent_id and not c.subagent_id
            )
            stored = await ctx.deps.conversation_manager.get(conversation.id)
            if thread.kind == "native_thread":
                await agent.validate_fork(stored, sender=sender)
            model = stored.runs[-1].model_name if stored.runs else thread.active_model
            ctx.state.decision = SummonDecision(
                agent_id=agent.id,
                model=None if thread.kind == "native_thread" else model,
                reason=f"Teleport requested from {requested_from}",
                hint=decision.hint,
                summon="",
            )
            return Teleport(
                request=TeleportRequest(
                    tool_call_id=None,
                    hint=decision.hint,
                    destination=decision.destination,
                    new_thread=decision.new_thread,
                    project=decision.project,
                    ref=decision.ref,
                    resume=decision.resume,
                    prompt=decision.prompt,
                ),
                origin=origin,
                agent_id=agent.id,
            )
        ctx.state.decision = decision
        ctx.state.run_name = "summon"
        ctx.state.handoff = PendingHandoff(
            source_agent_tentacle_id=agent_id,
            source_conversation_id=next(
                (
                    c.id
                    for c in thread.conversations
                    if c.agent_tentacle_id == agent_id and not c.subagent_id
                ),
                None,
            ),
        )
        return Summon()


# `Summon` and `Scheme` share an import cycle with `react`, which closes it at its
# own bottom — so they only exist once `react` has run. The `route` import above
# already pulls `react` in, and importing them here, after it, is what `react`
# itself does for the same reason.
from octomate.reflex.nodes.command import Command  # noqa: E402
from octomate.reflex.nodes.scheme import Scheme  # noqa: E402
from octomate.reflex.nodes.summon import Summon  # noqa: E402
from octomate.reflex.nodes.teleport import Teleport  # noqa: E402
