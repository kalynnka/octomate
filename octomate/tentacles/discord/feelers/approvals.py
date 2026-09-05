from __future__ import annotations

import re
import uuid
from typing import Self

import discord

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredApproval
from octomate.telemetry import channel_logfire
from octomate.tentacles.discord.feelers.actions import (
    DiscordActionUnavailable,
    DiscordComponentRouter,
)
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.deferred import ApprovalFeeler
from octomate.tentacles.feelers.output import IMMessageID

APPROVAL_CUSTOM_ID_TEMPLATE = re.compile(
    r"om:a:(?P<batch>[0-9a-f]{32}):(?P<action>[0-9a-f]{32}):(?P<approved>[01])"
)
APPROVAL_REQUEST_LIMIT = 1500
APPROVAL_HEADING_LIMIT = 100
APPROVAL_DESCRIPTION_LIMIT = 200


class DiscordApprovalButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=APPROVAL_CUSTOM_ID_TEMPLATE,
):
    def __init__(
        self,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        approved: bool,
        *,
        disabled: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.action_id = action_id
        self.approved = approved
        super().__init__(
            discord.ui.Button(
                label="Approve" if approved else "Deny",
                style=(
                    discord.ButtonStyle.success
                    if approved
                    else discord.ButtonStyle.danger
                ),
                custom_id=(f"om:a:{batch_id.hex}:{action_id.hex}:{int(approved)}"),
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[discord.Client],
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> Self:
        if not isinstance(item, discord.ui.Button):
            raise TypeError("Discord approval control is not a button")
        return cls(
            uuid.UUID(hex=match["batch"]),
            uuid.UUID(hex=match["action"]),
            match["approved"] == "1",
            disabled=item.disabled,
        )

    async def callback(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        await interaction.response.defer()
        router = DiscordComponentRouter.for_client(interaction.client)

        async def settle_message(action: DeferredApproval) -> None:
            await interaction.edit_original_response(
                content=approval_resolution_content(
                    action,
                    approved=self.approved,
                    responder_id=str(interaction.user.id),
                ),
                view=approval_view(action, disabled=True),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        try:
            await router.resolve_approval(
                batch_id=self.batch_id,
                action_id=self.action_id,
                responder_id=str(interaction.user.id),
                approved=self.approved,
                settle_message=settle_message,
            )
        except DiscordActionUnavailable as error:
            await interaction.followup.send(str(error), ephemeral=True)


class DiscordApprovalFeeler(ApprovalFeeler):
    def __init__(self, ink: DiscordInk) -> None:
        self.ink = ink

    @channel_logfire.instrument("discord.approvals.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredApproval],
    ) -> dict[uuid.UUID, IMMessageID | None]:
        message_ids: dict[uuid.UUID, IMMessageID | None] = {}
        chat_id = address.chat_id or address.user_id
        for action in actions:
            message_ids[action.id] = await self.ink.send_message(
                chat_id,
                address.chat_type,
                [
                    DiscordOutboundMessage(
                        content=approval_content(action),
                        view=approval_view(action),
                    )
                ],
                channel_thread_id=address.channel_thread_id or chat_id,
            )
        return message_ids


def approval_content(action: DeferredApproval) -> str:
    request = action.args.model_dump_json(indent=2, exclude_defaults=True)
    if len(request) > APPROVAL_REQUEST_LIMIT:
        request = f"{request[:APPROVAL_REQUEST_LIMIT].rstrip()}\n… (truncated)"
    title = action.args.title[:APPROVAL_HEADING_LIMIT]
    tool_name = action.args.tool_name[:APPROVAL_HEADING_LIMIT]
    description = (
        f"\n{action.args.description[:APPROVAL_DESCRIPTION_LIMIT]}\n"
        if action.args.description
        else "\n"
    )
    return f"**{title}: `{tool_name}`**{description}```json\n{request}\n```"


def approval_view(
    action: DeferredApproval,
    *,
    disabled: bool = False,
) -> discord.ui.View:
    if action.batch_id is None:
        raise ValueError("Discord approval controls require a batch id")
    view = discord.ui.View(timeout=None)
    view.add_item(
        DiscordApprovalButton(
            action.batch_id,
            action.id,
            True,
            disabled=disabled,
        )
    )
    view.add_item(
        DiscordApprovalButton(
            action.batch_id,
            action.id,
            False,
            disabled=disabled,
        )
    )
    return view


def approval_resolution_content(
    action: DeferredApproval,
    *,
    approved: bool,
    responder_id: str,
) -> str:
    status = "Approved" if approved else "Denied"
    tool_name = action.args.tool_name[:APPROVAL_HEADING_LIMIT]
    return f"**{tool_name}** — {status} by <@{responder_id}>"
