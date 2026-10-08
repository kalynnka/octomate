"""Execute explicit command intent on its selected conversation, without routing text."""

from contextlib import AsyncExitStack
from dataclasses import dataclass

from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.nodes.scheme import Scheme
from octomate.reflex.nodes.summon import Summon
from octomate.reflex.nodes.teleport import Teleport
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexState,
    ResponseTarget,
)
from octomate.schemas.awakes import CommandSignal
from octomate.schemas.commands import CommandError, CommandResult
from octomate.schemas.triage import AgentRouteKey
from octomate.telemetry import reflex_logfire


@dataclass
class Command(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """Own one command invocation through direct feedback or an ordinary agent stream."""

    signal: CommandSignal

    @reflex_logfire.instrument("reflex.command", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> Summon | Scheme | Teleport | End[ReflexGraphResult]:
        context = self.signal.context
        agent = ctx.deps.agent(context.agent_id)
        channel = ctx.deps.channel(context.address.channel_tentacle_id)
        state = ctx.state
        target = ResponseTarget(
            channel_id=channel.id,
            address=context.address,
            thread_strategy=channel.thread_strategy,
        )
        state.source_target = state.target = target
        state.user_profile = None
        state.run_name = "command"
        state.decision = None
        state.selection = AgentRouteKey(context.agent_id, context.model)
        conversation = context.conversation
        state.conversation_id = conversation.id if conversation else None
        state.thread = (
            await ctx.deps.thread_manager.get(
                conversation.thread_id, with_messages=False
            )
            if conversation is not None
            else None
        )
        runtime = ctx.deps.runtime
        session, suspender, capabilities = await runtime.resources(ctx)
        try:
            async with AsyncExitStack() as stack:
                validated = await stack.enter_async_context(
                    agent.commands.validate(
                        agent,
                        context,
                        self.signal.invocation,
                        delivery_id=self.signal.delivery_id,
                        session=session,
                    )
                )
                if isinstance(validated, CommandResult | CommandError):
                    await runtime.present_command(suspender, validated)
                    return End(validated)
                if validated.profile is not None:
                    await runtime.prepare_user(
                        ctx,
                        validated.profile,
                        session=session,
                        suspender=suspender,
                        capabilities=capabilities,
                    )
                output = await stack.enter_async_context(
                    agent.commands.execute(
                        agent,
                        context,
                        self.signal.invocation,
                        validated,
                        delivery_id=self.signal.delivery_id,
                        deferred_suspender=suspender,
                        capabilities=capabilities,
                    )
                )
                if isinstance(output, CommandResult | CommandError):
                    await runtime.present_command(suspender, output)
                    return End(output)
                state.decision = suspender.continuation_decision()
                result = await runtime.drive(ctx, suspender, output)
                await runtime.present_result(
                    ctx, suspender, result, streamed=channel.config.stream.enabled
                )
                return await runtime.finish(ctx, session, suspender, result)
        finally:
            if state.thread is not None and state.user_profile is not None:
                await ctx.deps.workspaces.save(state.thread)
