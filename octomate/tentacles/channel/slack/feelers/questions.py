from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import DeferredQuestion
from octomate.tentacles.channel.feelers.deferred import QuestionFeeler
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.cards import (
    QUESTION_CARD_ICON,
    card_block,
)
from octomate.tentacles.channel.slack.schema import (
    SlackBlock,
    SlackOutboundMessage,
    SlackQuestionActionValue,
    SlackQuestionState,
)

if TYPE_CHECKING:
    from octomate.tentacles.channel.slack.ink import SlackInk


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


class SlackAskQuestionFeeler(QuestionFeeler):
    def __init__(self, ink: SlackInk) -> None:
        self.ink = ink

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]:
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


def ask_question_blocks(
    actions: list[DeferredQuestion],
    *,
    page: int = 0,
    answers: dict[UUID, str] | None = None,
) -> list[SlackBlock]:
    if not actions:
        return []
    answers = answers or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    choices = list(action.args.get("choices") or [])[:MAX_QUESTION_CHOICES]
    hint = action.args.get("hint") or ""
    saved = answers.get(action.id, "")
    blocks: list[SlackBlock] = [
        card_block(
            title=f"*{action.args['question']}*",
            icon=QUESTION_CARD_ICON,
            subtitle=(
                f"Questions {page + 1} of {len(actions)}" if len(actions) > 1 else ""
            ),
            body=f"*Hint:* {hint}" if hint else "",
        )
    ]
    if choices:
        blocks.append(
            {
                "type": "input",
                "block_id": "choice_block",
                "dispatch_action": True,
                "optional": True,
                "element": choice_radio_buttons(choices, saved),
                "label": {"type": "plain_text", "text": "Choose one"},
            }
        )
    input_element: SlackBlock = {
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
                "text": "Other" if choices else "Answer",
            },
        }
    )
    nav: list[SlackBlock] = []
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
    nav_elements: list[JsonValue] = [*nav]
    actions_block: SlackBlock = {"type": "actions", "elements": nav_elements}
    blocks.append(actions_block)
    return blocks


def submitted_blocks(
    actions: list[DeferredQuestion],
    answers: dict[UUID, str] | None = None,
) -> list[SlackBlock]:
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
        card_block(
            title="Answers submitted",
            icon=QUESTION_CARD_ICON,
            subtitle=f"{count} {noun}",
            body=summary,
        )
    ]


def collect_current_answer(
    state: SlackQuestionState,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    *,
    prefer_choice: bool = False,
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
    if prefer_choice:
        answers[action_id] = choice or answer or answers.get(action_id, "")
    else:
        answers[action_id] = answer or choice or answers.get(action_id, "")
    return answers


def choice_radio_buttons(
    choices: list[str],
    saved: str,
) -> SlackBlock:
    options: list[JsonValue] = []
    initial_option: SlackBlock | None = None
    for choice in choices:
        option: SlackBlock = {
            "text": {"type": "plain_text", "text": str(choice)},
            "value": str(choice),
        }
        options.append(option)
        if option["value"] == saved:
            initial_option = option
    element: SlackBlock = {
        "type": "radio_buttons",
        "action_id": SlackBlockAction.ASK_QUESTION_CHOICE.value,
        "options": options,
    }
    if initial_option is not None:
        element["initial_option"] = initial_option
    return element


def question_button(
    text: str,
    action: SlackBlockAction | str,
    actions: list[DeferredQuestion],
    page: int,
    answers: dict[UUID, str],
    *,
    style: str | None = None,
) -> SlackBlock:
    batch_id = actions[0].batch_id
    if batch_id is None:
        raise ValueError("question buttons require a batch id")
    value: SlackQuestionActionValue = {
        "batch_id": batch_id,
        "questions": actions,
        "page": page,
        "answers": answers,
    }
    button: SlackBlock = {
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
