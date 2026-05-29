from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import (
    DeferredActionVariantAdapter,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.channel.feelers import (
    ApprovalFeeler,
    AskQuestionFeeler,
)
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage

if TYPE_CHECKING:
    from octomate.tentacles.channel.lark.ink import LarkInk


ACTION_CARD_FIELDS = {"kind", "tool_name", "args"}
ACTION_CARD_JSON_LIMIT = 2000


class LarkCardAction(StrEnum):
    APPROVAL_APPROVE = "approval_approve"
    APPROVAL_DENY = "approval_deny"
    ASK_QUESTION_BACK = "ask_question_back"
    ASK_QUESTION_NEXT = "ask_question_next"
    ASK_QUESTION_SUBMIT = "ask_question_submit"


class LarkApprovalFeeler(ApprovalFeeler):
    def __init__(self, ink: LarkInk) -> None:
        self.ink = ink

    async def present(
        self,
        key: ConversationKey,
        action: DeferredApproval,
    ) -> str | None:
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        return await self.ink.send_message(
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


class LarkAskQuestionFeeler(AskQuestionFeeler):
    def __init__(self, ink: LarkInk) -> None:
        self.ink = ink

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, str | None]:
        if not actions:
            return {}
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        message_id = await self.ink.send_message(
            key.chat_id or key.user_id,
            key.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=ask_question_card(actions),
                )
            ],
            reply_to,
            reply_in_thread=reply_to is not None,
        )
        return {action.id: message_id for action in actions}


def approval_card(action: DeferredApproval) -> str:
    return json.dumps(approval_card_data(action), ensure_ascii=False)


def approval_card_data(action: DeferredApproval) -> dict[str, Any]:
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
) -> dict[str, Any]:
    status = "Approved" if approved else "Denied"
    return {
        "header": {
            "title": {"tag": "plain_text", "content": status},
            "template": "green" if approved else "red",
        },
        "elements": [{"tag": "markdown", "content": f"**{tool_name}**\n\n{status}"}],
    }


def ask_question_card(
    actions: list[DeferredQuestion] | list[dict[str, Any]],
    *,
    page: int = 0,
    answers: dict[str, str] | None = None,
) -> str:
    questions = (
        actions
        if actions and isinstance(actions[0], dict)
        else question_payload(actions)  # type: ignore[arg-type]
    )
    return json.dumps(
        ask_question_card_data(
            questions=questions,  # type: ignore[arg-type]
            page=page,
            answers=answers,
        ),
        ensure_ascii=False,
    )


def ask_question_card_data(
    *,
    questions: list[dict[str, Any]],
    page: int = 0,
    answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    answers = answers or {}
    page = max(0, min(page, len(questions) - 1))
    question = questions[page]
    action_id = str(question["action_id"])
    choices = list(question.get("choices") or [])
    hint = str(question.get("hint") or "")
    text = str(question["question"])
    if hint:
        text = f"{text}\n\n_Hint: {hint}_"
    saved = answers.get(action_id, "")
    elements: list[dict[str, Any]] = []
    if choices:
        elements.append(
            {
                "tag": "select_static",
                "name": "choice",
                "placeholder": {"tag": "plain_text", "content": "Select an option"},
                "options": [
                    {
                        "text": {"tag": "plain_text", "content": str(choice)},
                        "value": str(choice),
                    }
                    for choice in choices
                ],
            }
        )
    input_element: dict[str, Any] = {
        "tag": "input",
        "name": "answer",
        "placeholder": {"tag": "plain_text", "content": "Type your answer"},
    }
    if saved and saved not in choices:
        input_element["default_value"] = saved
    elements.append(input_element)

    buttons: list[dict[str, Any]] = []
    if page > 0:
        buttons.append(
            nav_button(
                "Back",
                LarkCardAction.ASK_QUESTION_BACK,
                questions,
                page,
                answers,
            )
        )
    if page < len(questions) - 1:
        buttons.append(
            nav_button(
                "Next",
                LarkCardAction.ASK_QUESTION_NEXT,
                questions,
                page,
                answers,
                button_type="primary",
            )
        )
    else:
        buttons.append(
            nav_button(
                "Submit",
                LarkCardAction.ASK_QUESTION_SUBMIT,
                questions,
                page,
                answers,
                button_type="primary",
            )
        )
    elements.extend(buttons)
    return {
        "header": {
            "title": {"tag": "plain_text", "content": "Questions Needed"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"**Question {page + 1} of {len(questions)}**\n\n{text}",
            },
            {"tag": "hr"},
            {
                "tag": "form",
                "name": f"question_{action_id}",
                "elements": elements,
            },
        ],
    }


def submitted_card_data(questions: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(questions)
    noun = "question" if count == 1 else "questions"
    return {
        "header": {
            "title": {"tag": "plain_text", "content": "Answers Submitted"},
            "template": "green",
        },
        "elements": [
            {"tag": "markdown", "content": f"Answers submitted for **{count} {noun}**."}
        ],
    }


def collect_answer(value: dict[str, Any], form_value: dict[str, Any]) -> dict[str, str]:
    answers = dict(value.get("answers") or {})
    questions = value.get("questions") or []
    page = int(value.get("page") or 0)
    if not questions:
        return answers
    page = max(0, min(page, len(questions) - 1))
    action_id = str(questions[page]["action_id"])
    answer = str(form_value.get("answer") or "").strip()
    if not answer:
        answer = str(form_value.get("choice") or "").strip()
    answers[action_id] = answer
    return answers


def nav_button(
    text: str,
    action: LarkCardAction,
    questions: list[dict[str, Any]],
    page: int,
    answers: dict[str, str],
    *,
    button_type: str = "default",
) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "action_type": "form_submit",
        "name": action.value,
        "value": {
            "action": action.value,
            "batch_id": questions[0].get("batch_id", "") if questions else "",
            "questions": questions,
            "page": page,
            "answers": answers,
        },
    }


def question_payload(actions: list[DeferredQuestion]) -> list[dict[str, Any]]:
    return [
        {
            "batch_id": str(action.batch_id),
            "action_id": str(action.id),
            "question": action.args["question"],
            "hint": action.args.get("hint") or "",
            "choices": action.args.get("choices") or [],
        }
        for action in actions
    ]
