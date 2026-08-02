from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic_graph import BaseNode, End, GraphRunContext

from octomate.reflex.nodes.react import React
from octomate.reflex.state import (
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.schemas.triage import SchemeDecision, SummonDecision
from octomate.telemetry import reflex_logfire

logger = logging.getLogger(__name__)


@dataclass
class Scheme(BaseNode[ReflexState, ReflexDeps, ReflexGraphResult]):
    """A `scheme` call: hand this turn's brief to the asking user's direct messages.

    The receiver is whoever already owns that DM, or the channel's default agent when
    nobody does — never an agent this run picked, so a group can't point someone's
    private assistant somewhere. From there it is an ordinary handoff: the brief becomes
    the receiving agent's prompt and the handoff is recorded on the DM's own thread, the
    same way `Route` re-enters `React` with a decision it resolved itself.
    """

    request: SchemeDecision
    origin: ResponseTarget
    agent_id: str

    @reflex_logfire.instrument("reflex.scheme", extract_args=False)
    async def run(
        self,
        ctx: GraphRunContext[ReflexState, ReflexDeps],
    ) -> React | End[ReflexGraphResult]:
        state = ctx.state
        origin = self.origin
        if origin.address is None:
            raise ValueError("Scheme requires a resolved origin")
        origin_address = origin.address
        # Where the gate resolved this to: this channel's direct messages, or another
        # channel's, with the account there taken from the identity registry.
        target = self.request.destination
        channel = ctx.deps.channel(target.channel_tentacle_id)

        dm_address = await channel.open_dm(target.user_id)
        if dm_address is None:
            # The gate refused the cases we can know in advance, so this is the platform
            # failing at the moment of asking. Nothing has moved: leave the turn where
            # it is, with the origin agent's own reply already delivered.
            logger.warning(
                "Channel %s could not open a DM with %s; leaving the turn here",
                target.channel_tentacle_id,
                target.user_id,
            )
            return End(ReflexResult(decision=None, target=origin))

        dm_thread = await ctx.deps.thread_manager.ensure(dm_address)
        receiver = dm_thread.active_agent_tentacle_id
        # Resolved against the *target* channel: which agents serve a channel is that
        # channel's own config, so a cross-channel move lands on an agent configured
        # there rather than one carried over from where the request came from.
        resolved = ctx.deps.resolve_agent(
            target.channel_tentacle_id,
            receiver,
            dm_thread.active_model if receiver else None,
        )
        # Say so where it came from: a move into a DM posts nothing in the origin chat,
        # so without this the group is left watching silence while the work continues
        # somewhere it cannot see. (`summon thread` announces itself by opening the
        # thread with the same hint.)
        try:
            await ctx.deps.channel(origin).feelers.markdown.present(
                origin_address, self.request.hint
            )
        except Exception:
            logger.warning(
                "Channel %s failed to announce the move to a DM",
                origin.channel_id,
                exc_info=True,
            )

        state.run_name = "summon"
        state.thread = dm_thread
        state.target = ResponseTarget(
            channel_id=target.channel_tentacle_id,
            address=dm_address,
            thread_strategy=channel.thread_strategy,
            mode="main",
        )
        state.decision = SummonDecision(
            action="summon",
            agent_id=resolved.agent,
            model=resolved.model,
            destination="here",
            reason="Continuing with this user privately.",
            hint=self.request.hint,
            summon=self.request.brief,
        )
        state.claim_handoff = True
        state.handoff_from_agent_tentacle_id = self.agent_id
        return React()
