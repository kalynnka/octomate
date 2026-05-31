from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import TypeAdapter

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import DeferredQuestion
from octomate.tentacles.channel.feelers import AskQuestionFeeler
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.schema import (
    LarkOutboundMessage,
    LarkQuestionActionValue,
    LarkQuestionFormValue,
)

if TYPE_CHECKING:
    from octomate.tentacles.channel.lark.ink import LarkInk


QUESTION_STATE_FIELDS = {
    "id",
    "batch_id",
    "kind",
    "tool_name",
    "tool_call_id",
    "position",
    "args",
}
LarkQuestionActionsAdapter = TypeAdapter(list[DeferredQuestion])
LarkQuestionActionValueAdapter = TypeAdapter(LarkQuestionActionValue)


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


def ask_question_card(
    actions: list[DeferredQuestion],
    *,
    page: int = 0,
    answers: dict[UUID, str] | None = None,
) -> str:
    return json.dumps(
        ask_question_card_data(
            actions=actions,
            page=page,
            answers=answers,
        ),
        ensure_ascii=False,
    )


def ask_question_card_data(
    *,
    actions: list[DeferredQuestion],
    page: int = 0,
    answers: dict[UUID, str] | None = None,
) -> dict[str, Any]:
    answers = answers or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    choices = list(action.args.get("choices") or [])
    hint = str(action.args.get("hint") or "")
    text = action.args["question"]
    if hint:
        text = f"{text}\n\n_Hint: {hint}_"
    saved = answers.get(action.id, "")
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
                actions,
                page,
                answers,
            )
        )
    if page < len(actions) - 1:
        buttons.append(
            nav_button(
                "Next",
                LarkCardAction.ASK_QUESTION_NEXT,
                actions,
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
                actions,
                page,
                answers,
                button_type="primary",
            )
        )
    elements.extend(buttons)
    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "Question" if len(actions) == 1 else "Questions",
            },
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"**Question {page + 1} of {len(actions)}**\n\n{text}",
            },
            {"tag": "hr"},
            {
                "tag": "form",
                "name": f"question_{action.id}",
                "elements": elements,
            },
        ],
    }


def submitted_card_data(actions: list[DeferredQuestion]) -> dict[str, Any]:
    count = len(actions)
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


def collect_answer(
    actions: list[DeferredQuestion],
    page: int,
    form_value: LarkQuestionFormValue,
    answers: dict[UUID, str] | None = None,
) -> dict[UUID, str]:
    collected = dict(answers or {})
    if not actions:
        return collected
    page = max(0, min(page, len(actions) - 1))
    action_id = actions[page].id
    answer = str(form_value.get("answer") or "").strip()
    if not answer:
        answer = str(form_value.get("choice") or "").strip()
    collected[action_id] = answer
    return collected


def nav_button(
    text: str,
    action: LarkCardAction,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    *,
    button_type: str = "default",
) -> dict[str, Any]:
    batch_id = actions[0].batch_id
    if batch_id is None:
        raise ValueError("question buttons require a batch id")
    value: LarkQuestionActionValue = {
        "action": action.value,
        "batch_id": batch_id,
        "questions": actions,
        "page": page,
        "answers": answers,
    }
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "action_type": "form_submit",
        "name": action.value,
        "value": LarkQuestionActionValueAdapter.dump_python(
            value,
            mode="json",
            include={
                "action": True,
                "batch_id": True,
                "questions": {"__all__": QUESTION_STATE_FIELDS},
                "page": True,
                "answers": True,
            },
            exclude_defaults=True,
            exclude_none=True,
        ),
    }
