"""Lark question card: one interactive card holding a whole batch of questions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import UUID7, JsonValue, TypeAdapter

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion, QuestionAnswer
from octomate.telemetry import lark_logfire
from octomate.tentacles.feelers.deferred import (
    QuestionFeeler,
    answer_text,
    question_text,
)
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.lark.feelers import cards
from octomate.tentacles.lark.feelers.actions import LarkCardAction
from octomate.tentacles.lark.schema import (
    LarkInteractiveCard,
    LarkOutboundMessage,
    LarkQuestionActionValue,
    LarkQuestionFormValue,
)
from octomate.types.json import JsonObject

if TYPE_CHECKING:
    from octomate.tentacles.lark.ink import LarkInk


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
    """The batch as one interactive card: a form with a dropdown or a text input
    per question, all sent together by Submit."""

    def __init__(self, ink: LarkInk) -> None:
        self.ink = ink

    @lark_logfire.instrument("lark.ask_questions.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[UUID7, IMMessageID | None]:
        if not actions:
            return {}
        channel_thread_id = (
            address.channel_thread_id
            if address.channel_thread_id and address.channel_thread_id.startswith("om_")
            else address.chat_id or address.user_id
        )
        message_id = await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=ask_question_card(actions),
                )
            ],
            channel_thread_id=channel_thread_id,
            reply_in_thread=channel_thread_id is not None,
        )
        return {action.id: message_id for action in actions}


def ask_question_card(actions: list[DeferredQuestion]) -> str:
    return LarkInteractiveCard.model_validate(
        ask_question_card_data(actions)
    ).model_dump_json(by_alias=True, exclude_unset=True)


def ask_question_card_data(actions: list[DeferredQuestion]) -> JsonObject:
    """Every question in one form. A form keeps its inputs in the client until a
    `form_submit` button sends them, so nothing is sent before Submit; a choice
    is therefore a dropdown, since a button would send on click."""
    elements: list[JsonValue] = []
    for index, action in enumerate(actions):
        choices = list(action.args.get("choices") or [])
        hint = str(action.args.get("hint") or "")
        text = question_text(action)
        if len(actions) > 1:
            text = f"{index + 1}. {text}"
        elements.append(
            cards.markdown(f"**{text}**\n_{hint}_" if hint else f"**{text}**")
        )
        if choices:
            multi = action.args.get("multi_select", False)
            elements.append(
                {
                    "tag": "multi_select_static" if multi else "select_static",
                    "name": f"{'picks' if multi else 'choice'}_{index}",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "Pick any" if multi else "Pick one",
                    },
                    "options": [
                        {
                            "text": {"tag": "plain_text", "content": choice},
                            "value": choice,
                        }
                        for choice in choices
                    ],
                }
            )
        elements.append(
            {
                "tag": "input",
                "name": f"answer_{index}",
                "placeholder": {
                    "tag": "plain_text",
                    "content": "Or type another answer"
                    if choices
                    else "Type your answer",
                },
            }
        )
    # An `action` container drops form_submit buttons, so Submit sits in a column.
    elements.append(
        {
            "tag": "column_set",
            "columns": [{"tag": "column", "elements": [submit_button(actions)]}],
        }
    )
    return cards.simple_card(
        [{"tag": "form", "name": "questions", "elements": elements}],
        header=cards.header(
            "Question" if len(actions) == 1 else "Questions",
            template="blue",
        ),
    )


def submitted_card_data(
    actions: list[DeferredQuestion],
    answers: dict[UUID7, QuestionAnswer] | None = None,
) -> JsonObject:
    answers = answers or {}
    count = len(actions)
    noun = "question" if count == 1 else "questions"
    summary = "\n\n".join(
        f"**{index}. {question_text(action)}**\n"
        f"{answer_text(answers.get(action.id)) or '_No answer provided_'}"
        for index, action in enumerate(actions, start=1)
    )
    content = f"Answers submitted for **{count} {noun}**."
    if summary:
        content = f"{content}\n\n{summary}"
    return cards.simple_card(
        [cards.markdown(content)],
        header=cards.header("Answers Submitted", template="green"),
    )


def collect_answers(
    actions: list[DeferredQuestion],
    form_value: LarkQuestionFormValue,
) -> dict[UUID7, QuestionAnswer]:
    """Each question's answer from the submitted form: a multi-select question's
    picks over typed text, and typed text over a single pick."""
    answers: dict[UUID7, QuestionAnswer] = {}
    for index, action in enumerate(actions):
        picks = form_value.get(f"picks_{index}")
        typed = str(form_value.get(f"answer_{index}") or "").strip()
        choice = form_value.get(f"choice_{index}")
        if isinstance(picks, list) and picks:
            answers[action.id] = [*picks]
        elif typed:
            answers[action.id] = typed
        elif isinstance(choice, str) and choice:
            answers[action.id] = choice
    return answers


def submit_button(actions: list[DeferredQuestion]) -> JsonObject:
    batch_id = actions[0].batch_id
    if batch_id is None:
        raise ValueError("question buttons require a batch id")
    value: LarkQuestionActionValue = {
        "action": LarkCardAction.ASK_QUESTION_SUBMIT.value,
        "batch_id": batch_id,
        "questions": actions,
    }
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "Submit"},
        "type": "primary",
        "action_type": "form_submit",
        # Lark requires every element in a form to have a unique ``name``.
        "name": LarkCardAction.ASK_QUESTION_SUBMIT.value,
        "value": LarkQuestionActionValueAdapter.dump_python(
            value,
            mode="json",
            include={
                "action": True,
                "batch_id": True,
                "questions": {"__all__": QUESTION_STATE_FIELDS},
            },
            exclude_defaults=True,
            exclude_none=True,
        ),
    }
