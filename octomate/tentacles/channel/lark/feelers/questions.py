from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

import logfire
from pydantic import JsonValue, TypeAdapter

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion
from octomate.tentacles.channel.feelers.deferred import QuestionFeeler, question_text
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.lark.feelers import cards
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.schema import (
    LarkOutboundMessage,
    LarkQuestionActionValue,
    LarkQuestionFormValue,
)
from octomate.types.json import JsonObject

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


class LarkAskQuestionFeeler(QuestionFeeler):
    def __init__(self, ink: LarkInk) -> None:
        self.ink = ink

    @logfire.instrument("lark.ask_questions.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]:
        if not actions:
            return {}
        reply_to = address.thread_id if address.thread_id.startswith("om_") else None
        message_id = await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
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
) -> JsonObject:
    answers = answers or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    choices = list(action.args.get("choices") or [])
    hint = str(action.args.get("hint") or "")
    text = action.args["question"]
    if hint:
        text = f"{text}\n\n_Hint: {hint}_"
    saved = answers.get(action.id, "")
    # clicking one records it and advances to the next question (or
    # submits on the last). Free-text answers use the input + Next/Submit.
    elements: list[JsonValue] = [
        question_button(
            choice,
            LarkCardAction.ASK_QUESTION_CHOICE,
            actions,
            page,
            answers,
            button_type="primary" if choice == saved else "default",
            choice=choice,
            name=f"{LarkCardAction.ASK_QUESTION_CHOICE.value}_{index}",
        )
        for index, choice in enumerate(choices)
    ]
    input_element: JsonObject = {
        "tag": "input",
        "name": "answer",
        "placeholder": {
            "tag": "plain_text",
            "content": "Or type another answer" if choices else "Type your answer",
        },
    }
    if saved and saved not in choices:
        input_element["default_value"] = saved
    elements.append(input_element)

    # Back and Next/Submit share one horizontal action row.
    nav: list[JsonValue] = []
    if page > 0:
        nav.append(
            question_button(
                "Back",
                LarkCardAction.ASK_QUESTION_BACK,
                actions,
                page,
                answers,
            )
        )
    if page < len(actions) - 1:
        nav.append(
            question_button(
                "Next",
                LarkCardAction.ASK_QUESTION_NEXT,
                actions,
                page,
                answers,
                button_type="primary",
            )
        )
    else:
        nav.append(
            question_button(
                "Submit",
                LarkCardAction.ASK_QUESTION_SUBMIT,
                actions,
                page,
                answers,
                button_type="primary",
            )
        )
    # A `column_set` lays the nav buttons in one row; an `action` container drops
    # form_submit buttons, so each button gets its own column instead.
    elements.append(
        {
            "tag": "column_set",
            "columns": [{"tag": "column", "elements": [button]} for button in nav],
        }
    )
    return cards.simple_card(
        [
            cards.markdown(f"**Question {page + 1} of {len(actions)}**\n\n{text}"),
            cards.divider(),
            {
                "tag": "form",
                "name": f"question_{action.id}",
                "elements": elements,
            },
        ],
        header=cards.header(
            "Question" if len(actions) == 1 else "Questions",
            template="blue",
        ),
    )


def submitted_card_data(
    actions: list[DeferredQuestion],
    answers: dict[UUID, str] | None = None,
) -> JsonObject:
    answers = answers or {}
    count = len(actions)
    noun = "question" if count == 1 else "questions"
    summary = "\n\n".join(
        f"**{index}. {question_text(action)}**\n"
        f"{answers.get(action.id) or '_No answer provided_'}"
        for index, action in enumerate(actions, start=1)
    )
    content = f"Answers submitted for **{count} {noun}**."
    if summary:
        content = f"{content}\n\n{summary}"
    return cards.simple_card(
        [cards.markdown(content)],
        header=cards.header("Answers Submitted", template="green"),
    )


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
    # Choices are recorded via the choice buttons (kept in ``answers``); the
    # text input only overrides when the user actually typed something.
    answer = str(form_value.get("answer") or "").strip()
    if answer:
        collected[action_id] = answer
    return collected


def question_button(
    text: str,
    action: LarkCardAction,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    *,
    button_type: str = "default",
    choice: str | None = None,
    name: str | None = None,
) -> JsonObject:
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
    if choice is not None:
        value["choice"] = choice
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "action_type": "form_submit",
        # Lark requires every element in a form to have a unique ``name``;
        # choice buttons share the same action, so callers pass a distinct name.
        "name": name or action.value,
        "value": LarkQuestionActionValueAdapter.dump_python(
            value,
            mode="json",
            include={
                "action": True,
                "batch_id": True,
                "questions": {"__all__": QUESTION_STATE_FIELDS},
                "page": True,
                "answers": True,
                "choice": True,
            },
            exclude_defaults=True,
            exclude_none=True,
        ),
    }
