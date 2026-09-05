from __future__ import annotations

import logging

from pydantic_graph import GraphRunContext

from octomate.reflex.state import ReflexDeps, ReflexState
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import CrossingLanding

logger = logging.getLogger(__name__)


async def open_crossing(
    ctx: GraphRunContext[ReflexState, ReflexDeps],
    landing: CrossingLanding,
    source_address: ChannelAddress,
    hint_text: str,
    agent_tentacle_id: str,
) -> ChannelAddress | None:
    """Open a sub-thread of this person's direct messages on another channel and
    return where it landed, or None when nothing moved.

    Both spells that cross ask for the same thing and are refused the same way, so
    the sequence lives here rather than twice: the direct messages have to exist
    before there is anywhere to open a sub-thread of, and the origin has to be told,
    having otherwise watched the conversation leave without a word.

    The gate refused a channel that opens no sub-thread and one that cannot run the
    agent going there, so both failures left are the platform failing as it is asked.
    Neither falls back to the direct messages themselves: a `summon` landing there
    would pin an agent the origin chose onto someone's private conversation, and a
    `teleport` would fork a whole history onto a chat that already has one.
    """
    channel = ctx.deps.channel(landing.address.channel_tentacle_id)
    dm_address = await channel.open_dm(landing.address.user_id, hint_text)
    if dm_address is None:
        logger.warning(
            "Channel %s could not open a DM with %s; leaving the turn here",
            landing.address.channel_tentacle_id,
            landing.address.user_id,
        )
        return None
    try:
        opened = await channel.start_sub_thread(dm_address, hint_text)
    except Exception:
        logger.warning(
            "Channel %s raised starting a crossed sub-thread",
            landing.address.channel_tentacle_id,
            exc_info=True,
        )
        opened = dm_address
    if opened == dm_address:
        # An ink swallows its own send failures, so a failed open hands back the
        # address it was given rather than raising.
        logger.warning(
            "Channel %s opened no sub-thread for the crossing; leaving the turn "
            "here rather than claiming their direct messages",
            landing.address.channel_tentacle_id,
        )
        return None
    origin = source_address.channel_tentacle_id
    try:
        announced = await ctx.deps.channel(origin).feelers.markdown.present(
            source_address, hint_text
        )
    except Exception:
        logger.warning(
            "Channel %s failed to announce the crossing", origin, exc_info=True
        )
    else:
        await ctx.deps.record_move(
            source_address,
            hint_text,
            agent_tentacle_id=agent_tentacle_id,
            platform_message_id=announced,
        )
    return opened
