from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import TypeAdapter

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    DeferredActionVariantAdapter,
    DeferredApproval,
)
from octomate.telemetry import slack_logfire
from octomate.tentacles.channels.feelers.deferred import ApprovalFeeler
from octomate.tentacles.channels.feelers.output import IMMessageID
from octomate.tentacles.channels.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channels.slack.schema import (
    SlackApprovalActionValue,
    SlackBlock,
    SlackOutboundMessage,
)

if TYPE_CHECKING:
    from octomate.tentacles.channels.slack.ink import SlackInk


ACTION_REQUEST_FIELDS = {"kind", "tool_name", "args"}
ACTION_REQUEST_JSON_LIMIT = 2000
APPROVAL_STATE_FIELDS = {
    "id",
    "batch_id",
    "kind",
    "tool_name",
    "tool_call_id",
    "position",
    "args",
}
SlackApprovalActionsAdapter = TypeAdapter(list[DeferredApproval])
SlackApprovalActionValueAdapter = TypeAdapter(SlackApprovalActionValue)


class SlackApprovalFeeler(ApprovalFeeler):
    def __init__(self, ink: SlackInk) -> None:
        self.ink = ink

    @slack_logfire.instrument("slack.approvals.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]:
        if not actions:
            return {}
        text = approval_title(actions)
        message_id = await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [
                SlackOutboundMessage(
                    text=text,
                    markdown_text=text,
                    blocks=approval_blocks(actions),
                )
            ],
            address.channel_thread_id or None,
        )
        return {action.id: message_id for action in actions}


def approval_blocks(
    actions: list[DeferredApproval],
    *,
    page: int = 0,
    decisions: dict[UUID, bool] | None = None,
) -> list[SlackBlock]:
    if not actions:
        return []
    decisions = decisions or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    request_json = DeferredActionVariantAdapter.dump_json(
        action,
        indent=2,
        ensure_ascii=False,
        include=ACTION_REQUEST_FIELDS,
        exclude_defaults=True,
        exclude_none=True,
    ).decode()
    if len(request_json) > ACTION_REQUEST_JSON_LIMIT:
        request_json = request_json[:ACTION_REQUEST_JSON_LIMIT] + "\n... (truncated)"
    title = action.args.title or "Permission Required"
    description = action.args.description
    progress = (
        f"Approvals {page + 1} of {len(actions)}" if len(actions) > 1 else "Approval"
    )
    body = (
        f"{description}\n\n*Request:*\n```{request_json}```"
        if description
        else f"*Request:*\n```{request_json}```"
    )
    return [
        approval_progress_block(progress),
        approval_request_block(title, action.tool_name, body),
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                approval_button(
                    "Approve",
                    SlackBlockAction.APPROVAL_APPROVE,
                    actions,
                    page,
                    decisions,
                    style="primary",
                ),
                approval_button(
                    "Deny",
                    SlackBlockAction.APPROVAL_DENY,
                    actions,
                    page,
                    decisions,
                    style="danger",
                ),
            ],
        },
    ]


def approval_submitted_blocks(
    actions: list[DeferredApproval],
    decisions: dict[UUID, bool] | None = None,
    *,
    responder_id: str = "",
) -> list[SlackBlock]:
    count = len(actions)
    noun = "approval" if count == 1 else "approvals"
    decisions = decisions or {}
    metadata = f"_{count} {noun}_"
    if responder_id:
        metadata = f"{metadata}\n*By:* <@{responder_id}>"
    summary = "\n".join(
        (
            f"*{index}. `{action.tool_name}`* - "
            f"{'Approved' if decisions.get(action.id) else 'Denied'}"
        )
        for index, action in enumerate(actions, start=1)
        if action.id in decisions
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Approvals handled*\n{metadata}\n\n{summary}",
            },
        }
    ]


def approval_progress_block(progress: str) -> SlackBlock:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{progress}*"},
    }


def approval_request_block(title: str, tool_name: str, body: str) -> SlackBlock:
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*{title}: `{tool_name}`*\n\n{body}",
        },
    }


def approval_button(
    text: str,
    action: SlackBlockAction,
    actions: list[DeferredApproval],
    page: int,
    decisions: dict[UUID, bool],
    *,
    style: str,
) -> SlackBlock:
    batch_id = actions[0].batch_id
    if batch_id is None:
        raise ValueError("approval buttons require a batch id")
    value: SlackApprovalActionValue = {
        "batch_id": batch_id,
        "approvals": actions,
        "page": page,
        "decisions": decisions,
    }
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "style": style,
        "action_id": action.value,
        "value": json.dumps(
            SlackApprovalActionValueAdapter.dump_python(
                value,
                mode="json",
                include={
                    "batch_id": True,
                    "approvals": {"__all__": APPROVAL_STATE_FIELDS},
                    "page": True,
                    "decisions": True,
                },
                exclude_defaults=True,
                exclude_none=True,
            )
        ),
    }


def approval_title(actions: list[DeferredApproval]) -> str:
    count = len(actions)
    noun = "approval" if count == 1 else "approvals"
    return f"Octomate needs {count} {noun}"
