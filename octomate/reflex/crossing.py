"""Create a channel-owned destination and announce the move at its origin."""

from __future__ import annotations

import logging

from pydantic_graph import GraphRunContext

from octomate.capabilities.harness.events import GatewayEvent
from octomate.reflex.state import ReflexDeps, ReflexState
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.user import UserProfile

logger = logging.getLogger(__name__)


async def open_crossing(
    ctx: GraphRunContext[ReflexState, ReflexDeps],
    destination: ChannelAddress,
    source_address: ChannelAddress,
    hint_text: str,
    agent_tentacle_id: str,
) -> ChannelAddress | None:
    """Ask the destination channel to create an isolated thread, then announce it.

    The existing opener is recorded with its requesting user so the destination
    remains visible in their console. No fallback may reuse an existing chat.
    """
    channel = ctx.deps.channel(destination.channel_tentacle_id)
    profile = ctx.state.user_profile
    if profile is not None:
        if profile.channel_tentacle_id != channel.id:
            linked = await ctx.deps.thread_manager.users.profile(
                channel.id, destination.user_id
            )
            if linked is not None and linked.user_id == profile.user_id:
                profile = linked
            elif channel.thread_user_id(profile) != destination.user_id:
                raise ValueError(
                    "The destination profile is no longer linked to this user."
                )
    try:
        opened = await channel.start_thread(destination, hint_text)
    except Exception:
        logger.warning(
            "Channel %s could not create the destination thread",
            channel.id,
            exc_info=True,
        )
        return None
    if destination == source_address:
        await ctx.deps.record_move(
            source_address,
            hint_text,
            agent_tentacle_id=agent_tentacle_id,
            platform_message_id=opened.channel_thread_id,
        )
        # The thread hangs from its opener there, which is the move's line.
        await ctx.deps.announce(
            ctx.state,
            source_address,
            GatewayEvent(action="teleport", destination=opened),
        )
        return opened
    if profile is not None:
        if profile.channel_tentacle_id != channel.id:
            profile = UserProfile(
                channel_tentacle_id=channel.id,
                channel_user_id=opened.user_id,
                user_id=profile.user_id,
                name=profile.name,
                nickname=profile.nickname,
            )
        ctx.state.user_profile = profile
        # Attribute the existing opener so the requesting user can find the destination.
        # Native teleport's transcript fork already publishes its attributed fork notice.
        native_fork = (
            ctx.state.run_name == "teleport"
            and ctx.state.thread is not None
            and ctx.state.thread.kind == "native_thread"
        )
        if not native_fork:
            await ctx.deps.thread_manager.record_outbound(
                opened,
                agent_tentacle_id=agent_tentacle_id,
                sender=ctx.state.user_profile,
                actor_kind="system",
                segments=[MarkdownSegment(data={"text": hint_text})],
            )
    origin = source_address.channel_tentacle_id
    try:
        announced = await ctx.deps.announce(
            ctx.state,
            source_address,
            GatewayEvent(action="teleport", destination=opened, announcement=hint_text),
        )
    except Exception:
        logger.warning(
            "Channel %s failed to announce the crossing", origin, exc_info=True
        )
        return opened
    if origin in ctx.deps.channels:
        await ctx.deps.record_move(
            source_address,
            hint_text,
            agent_tentacle_id=agent_tentacle_id,
            platform_message_id=announced,
        )
    return opened
