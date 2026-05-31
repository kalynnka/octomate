from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import TypeAdapter

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
from octomate.tentacles.channel.slack.schema import (
    SlackOutboundMessage,
    SlackQuestionActionValue,
    SlackQuestionState,
)

if TYPE_CHECKING:
    from octomate.tentacles.channel.slack.ink import SlackInk


ACTION_CARD_FIELDS = {"kind", "tool_name", "args"}
ACTION_CARD_JSON_LIMIT = 2000
MAX_QUESTION_CHOICES = 3
QUESTION_STATE_FIELDS = {
    "id",
    "batch_id",
    "kind",
    "tool_name",
    "tool_call_id",
    "position",
    "args",
}
SlackQuestionActionsAdapter = TypeAdapter(list[DeferredQuestion])
SlackQuestionActionValueAdapter = TypeAdapter(SlackQuestionActionValue)


class SlackBlockAction(StrEnum):
    APPROVAL_APPROVE = "octomate_approval_approve"
    APPROVAL_DENY = "octomate_approval_deny"
    ASK_QUESTION_BACK = "octomate_question_back"
    ASK_QUESTION_NEXT = "octomate_question_next"
    ASK_QUESTION_SUBMIT = "octomate_question_submit"
    ASK_QUESTION_CHOICE = "octomate_question_choice"
    ASK_QUESTION_ANSWER = "octomate_question_answer"


class SlackApprovalFeeler(ApprovalFeeler):
    def __init__(self, ink: SlackInk) -> None:
        self.ink = ink

    async def present(
        self,
        key: ConversationKey,
        action: DeferredApproval,
    ) -> str | None:
        text = f"Permission required: {action.tool_name}"
        return await self.ink.send_message(
            key.chat_id or key.user_id,
            key.chat_type,
            [
                SlackOutboundMessage(
                    text=text,
                    markdown_text=text,
                    blocks=approval_blocks(action),
                )
            ],
            key.thread_id or None,
        )


class SlackAskQuestionFeeler(AskQuestionFeeler):
    def __init__(self, ink: SlackInk) -> None:
        self.ink = ink

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, str | None]:
        if not actions:
            return {}
        text = question_title(actions)
        message_id = await self.ink.send_message(
            key.chat_id or key.user_id,
            key.chat_type,
            [
                SlackOutboundMessage(
                    text=text,
                    markdown_text=text,
                    blocks=ask_question_blocks(actions),
                )
            ],
            key.thread_id or None,
        )
        return {action.id: message_id for action in actions}


def approval_blocks(action: DeferredApproval) -> list[dict[str, Any]]:
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
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Permission Required"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Tool:* `{action.tool_name}`\n*Request:*\n```{request_json}```"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": SlackBlockAction.APPROVAL_APPROVE.value,
                    "value": json.dumps(
                        {
                            "batch_id": str(action.batch_id),
                            "action_id": str(action.id),
                            "tool_name": action.tool_name,
                            "approved": True,
                        }
                    ),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": SlackBlockAction.APPROVAL_DENY.value,
                    "value": json.dumps(
                        {
                            "batch_id": str(action.batch_id),
                            "action_id": str(action.id),
                            "tool_name": action.tool_name,
                            "approved": False,
                        }
                    ),
                },
            ],
        },
    ]


def approval_resolution_blocks(
    *,
    tool_name: str,
    approved: bool,
    responder_id: str,
) -> list[dict[str, Any]]:
    status = "Approved" if approved else "Denied"
    byline = f"\n*By:* <@{responder_id}>" if responder_id else ""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{tool_name}* - {status}{byline}"},
        }
    ]


def ask_question_blocks(
    actions: list[DeferredQuestion],
    *,
    page: int = 0,
    answers: dict[UUID, str] | None = None,
) -> list[dict[str, Any]]:
    if not actions:
        return []
    answers = answers or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    choices = list(action.args.get("choices") or [])[:MAX_QUESTION_CHOICES]
    hint = action.args.get("hint") or ""
    saved = answers.get(action.id, "")
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": question_heading(actions)},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Question {page + 1} of {len(actions)}"}
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{action.args['question']}*"},
        },
    ]
    if hint:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*Hint:* {hint}"}],
            }
        )
    if choices:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    choice_button(
                        choice,
                        actions,
                        page,
                        answers,
                        saved,
                        choice_index,
                    )
                    for choice_index, choice in enumerate(choices)
                ],
            }
        )
    input_element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": SlackBlockAction.ASK_QUESTION_ANSWER.value,
        "multiline": not bool(choices),
        "placeholder": {
            "type": "plain_text",
            "text": "Optional note or other answer" if choices else "Type an answer",
        },
    }
    if choices:
        input_element["max_length"] = 160
    if saved and saved not in choices:
        input_element["initial_value"] = saved
    blocks.append(
        {
            "type": "input",
            "block_id": "answer_block",
            "optional": True,
            "element": input_element,
            "label": {
                "type": "plain_text",
                "text": "Details" if choices else "Answer",
            },
        }
    )
    nav: list[dict[str, Any]] = []
    if page > 0:
        nav.append(
            question_button(
                "Back",
                SlackBlockAction.ASK_QUESTION_BACK,
                actions,
                page,
                answers,
            )
        )
    if page < len(actions) - 1:
        nav.append(
            question_button(
                "Next",
                SlackBlockAction.ASK_QUESTION_NEXT,
                actions,
                page,
                answers,
                style="primary",
            )
        )
    else:
        nav.append(
            question_button(
                "Submit",
                SlackBlockAction.ASK_QUESTION_SUBMIT,
                actions,
                page,
                answers,
                style="primary",
            )
        )
    blocks.append({"type": "actions", "elements": nav})
    return blocks


def submitted_blocks(
    actions: list[DeferredQuestion],
    answers: dict[UUID, str] | None = None,
) -> list[dict[str, Any]]:
    count = len(actions)
    noun = "question" if count == 1 else "questions"
    answers = answers or {}
    summary = "\n\n".join(
        (
            f"*{index}. {action.args['question']}*\n"
            f"{answers.get(action.id) or '[No answer provided]'}"
        )
        for index, action in enumerate(actions, start=1)
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Answers submitted for *{count} {noun}*:\n\n{summary}",
            },
        }
    ]


def collect_current_answer(
    state: SlackQuestionState,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    selected_answer: str | None = None,
) -> dict[UUID, str]:
    if not actions:
        return answers
    page = max(0, min(page, len(actions) - 1))
    action_id = actions[page].id
    answer = ""
    choice = ""
    for block in state["values"].values():
        if SlackBlockAction.ASK_QUESTION_ANSWER.value in block:
            answer = str(
                block[SlackBlockAction.ASK_QUESTION_ANSWER.value].get("value") or ""
            ).strip()
        if SlackBlockAction.ASK_QUESTION_CHOICE.value in block:
            select_state = block[SlackBlockAction.ASK_QUESTION_CHOICE.value]
            if "selected_option" in select_state and select_state["selected_option"]:
                choice = str(select_state["selected_option"]["value"]).strip()
    answers[action_id] = (
        selected_answer or answer or choice or answers.get(action_id, "")
    )
    return answers


def choice_button(
    choice: Any,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    saved: str,
    choice_index: int,
) -> dict[str, Any]:
    choice_text = str(choice)
    action_id = actions[page].id
    next_answers = {**answers, action_id: choice_text}
    return question_button(
        choice_text,
        f"{SlackBlockAction.ASK_QUESTION_CHOICE.value}:{choice_index}",
        actions,
        page,
        next_answers,
        selected_answer=choice_text,
        style="primary" if saved == choice_text else None,
    )


def question_button(
    text: str,
    action: SlackBlockAction | str,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    *,
    selected_answer: str | None = None,
    style: str | None = None,
) -> dict[str, Any]:
    batch_id = actions[0].batch_id
    if batch_id is None:
        raise ValueError("question buttons require a batch id")
    value: SlackQuestionActionValue = {
        "batch_id": batch_id,
        "questions": actions,
        "page": page,
        "answers": answers,
    }
    if selected_answer is not None:
        value["selected_answer"] = selected_answer
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action.value if isinstance(action, SlackBlockAction) else action,
        "value": json.dumps(
            SlackQuestionActionValueAdapter.dump_python(
                value,
                mode="json",
                include={
                    "batch_id": True,
                    "questions": {"__all__": QUESTION_STATE_FIELDS},
                    "page": True,
                    "answers": True,
                    "selected_answer": True,
                },
                exclude_defaults=True,
                exclude_none=True,
            )
        ),
    }
    if style:
        button["style"] = style
    return button


def question_title(actions: list[DeferredQuestion]) -> str:
    count = len(actions)
    noun = "question" if count == 1 else "questions"
    return f"Octomate needs {count} {noun} answered"


def question_heading(actions: list[DeferredQuestion]) -> str:
    return "Question" if len(actions) == 1 else "Questions"
