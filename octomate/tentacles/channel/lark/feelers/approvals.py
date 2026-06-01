from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import (
    DeferredActionVariantAdapter,
    DeferredApproval,
)
from octomate.tentacles.channel.feelers.deferred import ApprovalFeeler
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.tentacles.channel.lark.ink import LarkInk


ACTION_CARD_FIELDS = {"kind", "tool_name", "args"}
ACTION_CARD_JSON_LIMIT = 2000


class LarkApprovalFeeler(ApprovalFeeler):
    def __init__(self, ink: LarkInk) -> None:
        self.ink = ink

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]:
        if not actions:
            return {}
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        message_ids: dict[UUID, IMMessageID | None] = {}
        for action in actions:
            message_ids[action.id] = await self.ink.send_message(
                key.chat_id or key.user_id,
                key.chat_type,
                [
                    LarkOutboundMessage(
                        msg_type="interactive",
                        content=approval_card(action),
                    )
                ],
                reply_to,
                reply_in_thread=reply_to is not None,
            )
        return message_ids


def approval_card(action: DeferredApproval) -> str:
    return json.dumps(approval_card_data(action), ensure_ascii=False)


def approval_card_data(action: DeferredApproval) -> JsonObject:
    request_json = DeferredActionVariantAdapter.dump_json(
        action,
        indent=2,
        ensure_ascii=False,
        include=ACTION_CARD_FIELDS,
        exclude_defaults=True,
        exclude_none=True,
    ).decode()
    if len(request_json) > ACTION_CARD_JSON_LIMIT:
        request_json = request_json[:ACTION_CARD_JSON_LIMIT] + "\n... (truncated)"
    return {
        "header": {
            "title": {"tag": "plain_text", "content": "Permission Required"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**Tool:** `{action.tool_name}`\n"
                    f"**Request:**\n```json\n{request_json}\n```"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Approve"},
                        "type": "primary",
                        "value": {
                            "action": LarkCardAction.APPROVAL_APPROVE.value,
                            "batch_id": str(action.batch_id),
                            "action_id": str(action.id),
                            "tool_name": action.tool_name,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Deny"},
                        "type": "danger",
                        "value": {
                            "action": LarkCardAction.APPROVAL_DENY.value,
                            "batch_id": str(action.batch_id),
                            "action_id": str(action.id),
                            "tool_name": action.tool_name,
                        },
                    },
                ],
            },
        ],
    }


def approval_resolution_card_data(
    *,
    tool_name: str,
    approved: bool,
) -> JsonObject:
    status = "Approved" if approved else "Denied"
    return {
        "header": {
            "title": {"tag": "plain_text", "content": status},
            "template": "green" if approved else "red",
        },
        "elements": [{"tag": "markdown", "content": f"**{tool_name}**\n\n{status}"}],
    }
