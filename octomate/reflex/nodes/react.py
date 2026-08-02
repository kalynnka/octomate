from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass

from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.events import MessageSentEvent, StreamEvents
from octomate.reflex.state import (
    DeferredResult,
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
)
from octomate.reflex.suspender import HumanReviewSuspender
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.triage import SchemeDecision, SummonDecision
from octomate.telemetry import reflex_logfire
from octomate.tentacles.channel.base import ChannelOutput
from octomate.tentacles.channel.feelers.output import split_reply

logger = logging.getLogger(__name__)


@dataclass
class React(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    resume_batch_id: uuid.UUID | None = None
    # Set by Teleport to resume the same agent against the forked history.
    teleport_results: DeferredToolResults | None = None

    @reflex_logfire.instrument("reflex.react", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> Handoff | Teleport | Scheme | End[ReflexGraphResult]:
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
        source_address = source_target.address
        resolved = ctx.deps.resolve_agent(
            source_address.channel_tentacle_id, decision.agent_id, decision.model
        )
        model = resolved.model
        decision = decision.model_copy(
            update={"agent_id": resolved.agent, "model": model}
        )
        state.decision = decision
        agent = ctx.deps.agent(resolved.agent)
        run_model = agent.models.get(model)
        if run_model is None:
            raise ValueError(f"agent {agent.id!r} has no configured model {model!r}")
        thread_id = state.thread.id if state.thread else None
        if state.thread is not None and state.claim_handoff:
            target_conversation = await ctx.deps.conversation_manager.ensure(
                state.thread.id,
                agent_tentacle_id=agent.id,
            )
            latest_handoff = state.thread.latest_handoff
            target_model = model
            if (
                latest_handoff is None
                or latest_handoff.to_agent_tentacle_id != agent.id
                or latest_handoff.to_model != target_model
            ):
                await ctx.deps.thread_manager.record_handoff(
                    state.thread,
                    from_agent_tentacle_id=state.handoff_from_agent_tentacle_id,
                    to_agent_tentacle_id=agent.id,
                    to_model=target_model,
                    reason=decision.reason,
                    hint=decision.hint,
                    brief=decision.summon,
                    target_conversation_id=target_conversation.id,
                )
            state.claim_handoff = False
            state.handoff_from_agent_tentacle_id = None
        target_channel = ctx.deps.channel(target)
        routes = [
            route
            for route in ctx.deps.available_routes[source_address.channel_tentacle_id]
            if route.agent_id != agent.id
        ]
        state.summon_routes = routes
        gate = GatewayCapability(
            routes=routes,
            current_agent_id=agent.id,
            # Every channel, not just this one: the gate reads `surfaces` off the
            # address's own channel to know whether `scheme` can land, and asks the
            # same of the others when it works out where else this person is.
            channels=ctx.deps.channels,
            users=ctx.deps.thread_manager.users,
            user_profile=state.user_profile,
            # The accomplice spells need to actually run one; without
            # a thread there is nowhere for a child conversation to live, and
            # the gate then simply does not offer them.
            agents=ctx.deps.agents,
            conversations=ctx.deps.conversation_manager,
            thread_id=thread_id,
            conversation_address=target_address,
        )
        user_capabilities = (
            await agent.user_capabilities(state.user_profile)
            if state.user_profile is not None
            else []
        )

        deferred_results = self.teleport_results
        if self.resume_batch_id is not None:
            batch = await ctx.deps.action_manager.get_batch(self.resume_batch_id)
            deferred_results = batch.build_results()
        if deferred_results is not None:
            user_prompt: str | Sequence[UserContent] | None = None
        else:
            user_prompt = decision.summon or str(state.user_prompt or "")

        suspender = HumanReviewSuspender(
            channel=target_channel,
            action_manager=ctx.deps.action_manager,
            conversation_manager=ctx.deps.conversation_manager,
            agent_tentacle_id=agent.id,
            run_name=state.run_name,
            source_address=source_address,
            target_address=target_address,
            target_mode=target.mode,
            decision=state.decision,
            thread_id=thread_id,
            emit_on_stream=target_channel.config.stream.enabled,
        )

        with reflex_logfire.span(
            "react",
            channel_id=target_address.channel_tentacle_id,
            agent_id=agent.id,
            conversation_address=str(target_address),
            streaming=target_channel.config.stream.enabled,
            resume_batch_id=str(self.resume_batch_id) if self.resume_batch_id else None,
        ) as span:
            stream_results: list[AgentRunResult[ChannelOutput]] = []
            reply_thread_message_ids: list[uuid.UUID] = []
            assistant_replies_bound = False
            if target_channel.config.stream.enabled:

                async def stream_events() -> AsyncIterator[
                    StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
                ]:
                    async with agent.run_stream_events(
                        user_prompt,
                        conversation_address=target_address,
                        thread_id=thread_id,
                        source_thread_address=state.source_thread_address,
                        source_thread_message_ids=state.source_thread_message_ids,
                        run_name=state.run_name,
                        model=run_model,
                        effort=decision.effort,
                        deferred_tool_results=deferred_results,
                        deferred_suspender=suspender,
                        capabilities=[gate, *user_capabilities],
                    ) as stream:
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                stream_results.append(event.result)
                            # A send bound somewhere other than this conversation.
                            # The timeline renders one surface, so anything else has
                            # to be delivered here and kept off the stream. Where is
                            # already settled: the gate resolved and refused, so this
                            # only addresses what it was handed.
                            if (
                                isinstance(event, MessageSentEvent)
                                and event.destination is not None
                            ):
                                destination = event.destination
                                destination_channel = ctx.deps.channel(
                                    destination.channel_tentacle_id
                                )
                                dm = await destination_channel.open_dm(
                                    destination.user_id
                                )
                                if dm is not None:
                                    # A bare message, with nobody taking the work up
                                    # there — what separates this from `scheme`. The
                                    # ledger row touches no conversation's model
                                    # messages, so the DM's own agent meets it as
                                    # pending context on its next turn.
                                    await destination_channel.feelers.segments.present(
                                        dm, event.segments
                                    )
                                    await ctx.deps.thread_manager.record_outbound(
                                        dm,
                                        agent_tentacle_id=agent.id,
                                        segments=event.segments,
                                        sender=destination_channel.self_profile,
                                    )
                                    continue
                                logger.warning(
                                    "Channel %s could not open a DM with %s; "
                                    "delivering the send to %s instead",
                                    destination.channel_tentacle_id,
                                    destination.user_id,
                                    target_address,
                                )
                            yield event

                try:
                    async with target_channel.feelers.timeline.open(
                        target_address
                    ) as timeline_state:
                        await timeline_state.drive(stream_events())
                except AgentRunError:
                    # A model/provider failure (e.g. invalid Bedrock credentials)
                    # surfaces here from the run stream itself, not the render. It
                    # is the real cause — let it propagate instead of masking it as
                    # a render warning and a generic "no result" error below.
                    raise
                except Exception:
                    logger.warning(
                        "Channel %s: timeline render failed",
                        target_address.channel_tentacle_id,
                        exc_info=True,
                    )
                if not stream_results:
                    raise RuntimeError(
                        f"react stream for {target_address} completed without a result"
                    )
                run_result = stream_results[-1]
                run_output: ChannelOutput = run_result.output
                if isinstance(run_output, str):
                    if run_output:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=[MarkdownSegment(data={"text": run_output})],
                            sender=target_channel.self_profile,
                            message_text=run_output,
                            raw=run_output,
                        )
                        reply_thread_message_ids.append(thread_message.id)
                elif isinstance(run_output, Iterable):
                    segments = list(run_output)
                    _reply_to, body = split_reply(segments)
                    if body:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=body,
                            sender=target_channel.self_profile,
                            raw="\n\n".join(str(segment) for segment in body),
                        )
                        reply_thread_message_ids.append(thread_message.id)
            else:
                run_result = await agent.run(
                    user_prompt,
                    conversation_address=target_address,
                    thread_id=thread_id,
                    source_thread_address=state.source_thread_address,
                    source_thread_message_ids=state.source_thread_message_ids,
                    run_name=state.run_name,
                    model=run_model,
                    effort=decision.effort,
                    deferred_tool_results=deferred_results,
                    deferred_suspender=suspender,
                    capabilities=[gate, *user_capabilities],
                )
                run_output: ChannelOutput = run_result.output
                if isinstance(run_output, str):
                    if run_output:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=[MarkdownSegment(data={"text": run_output})],
                            sender=target_channel.self_profile,
                            message_text=run_output,
                            raw=run_output,
                        )
                        reply_thread_message_ids.append(thread_message.id)
                        await ctx.deps.thread_manager.bind_assistant_replies(
                            reply_thread_message_ids,
                            run_id=run_result.run_id,
                        )
                        assistant_replies_bound = True
                        message_id = await target_channel.feelers.markdown.present(
                            target_address,
                            run_output,
                        )
                        await ctx.deps.thread_manager.mark_presented(
                            thread_message,
                            message_id,
                        )
                elif isinstance(run_output, Iterable):
                    # A segment list is the only media-bearing reply: deliver it
                    # natively (channels without a media transport fall back to
                    # the joined text form in send_segments).
                    segments = list(run_output)
                    _reply_to, body = split_reply(segments)
                    thread_message = None
                    if body:
                        thread_message = await ctx.deps.thread_manager.record_outbound(
                            target_address,
                            agent_tentacle_id=agent.id,
                            segments=body,
                            sender=target_channel.self_profile,
                            raw="\n\n".join(str(segment) for segment in body),
                        )
                        reply_thread_message_ids.append(thread_message.id)
                    await ctx.deps.thread_manager.bind_assistant_replies(
                        reply_thread_message_ids,
                        run_id=run_result.run_id,
                    )
                    assistant_replies_bound = True
                    message_id = await target_channel.feelers.segments.present(
                        target_address, segments
                    )
                    if thread_message is not None:
                        await ctx.deps.thread_manager.mark_presented(
                            thread_message,
                            message_id,
                        )
                # DeferredToolRequests / None: nothing to deliver here.

            output = run_result.output
            if self.resume_batch_id is not None:
                await ctx.deps.action_manager.mark_batch(
                    self.resume_batch_id,
                    "completed",
                    completed=True,
                )

            span.set_attribute("react.run_id", run_result.run_id)
            span.set_attribute(
                "react.deferred", isinstance(output, DeferredToolRequests)
            )
            if not assistant_replies_bound:
                await ctx.deps.thread_manager.bind_assistant_replies(
                    reply_thread_message_ids,
                    run_id=run_result.run_id,
                )
            if isinstance(output, DeferredToolRequests):
                # `teleport` is resolved by the graph (fork + resume), not a human. The
                # suspender classified it by its declared metadata kind and stashed it,
                # so route on the typed request instead of re-scanning tool names.
                if suspender.teleport is not None:
                    return Teleport(
                        request=suspender.teleport, origin=target, agent_id=agent.id
                    )
                return End(
                    DeferredResult(
                        requests=output,
                        target=target,
                        run_name=state.run_name,
                        result=run_result,
                        batch_id=suspender.suspended_batch_id,
                    )
                )

            gate_decision = gate.decision
            if isinstance(gate_decision, SchemeDecision):
                span.set_attribute("react.action", gate_decision.action)
                reflex_logfire.info(
                    "react -> scheme into the asker's dm",
                    hint=gate_decision.hint,
                )
                return Scheme(
                    request=gate_decision,
                    origin=target,
                    agent_id=agent.id,
                )
            if gate_decision is not None:
                state.decision = gate_decision
                state.target = target
                state.claim_handoff = True
                state.handoff_from_agent_tentacle_id = agent.id
                state.run_name = "summon"
                span.set_attribute("react.action", gate_decision.action)
                span.set_attribute("react.next_agent_id", gate_decision.agent_id)
                reflex_logfire.info(
                    "react -> {action} agent={agent_id}",
                    action=gate_decision.action,
                    agent_id=gate_decision.agent_id,
                    reason=gate_decision.reason,
                )
                return Handoff()

            return End(
                ReflexResult(
                    decision=decision,
                    target=target,
                    result=run_result,
                )
            )


# React and the three nodes it hands off to name each other in their `run` return
# hints, and pydantic-graph resolves those hints against this module's globals when
# the graph is built — so `if TYPE_CHECKING` is not enough, the names must really be
# here. Importing them at the top would deadlock the cycle (react would be half-built
# when handoff asked for it), so the cycle is closed here instead, after `React`
# exists. `nodes/__init__` imports this module first to keep that order.
from octomate.reflex.nodes.handoff import Handoff  # noqa: E402
from octomate.reflex.nodes.scheme import Scheme  # noqa: E402
from octomate.reflex.nodes.teleport import Teleport  # noqa: E402
