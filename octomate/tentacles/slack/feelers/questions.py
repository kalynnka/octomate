"""Slack question card: one Block Kit message holding a whole batch of questions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    DeferredQuestion,
    QuestionAnswer,
)
from octomate.telemetry import slack_logfire
from octomate.tentacles.feelers.deferred import (
    QuestionFeeler,
    answer_text,
    question_text,
)
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.slack.schema import (
    SlackBlock,
    SlackOutboundMessage,
    SlackQuestionActionValue,
    SlackQuestionState,
)

if TYPE_CHECKING:
    from octomate.tentacles.slack.ink import SlackInk


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
    """The batch as one Block Kit message: radio buttons, checkboxes or a text
    input per question, all read together when Submit is pressed."""

    def __init__(self, ink: SlackInk) -> None:
        self.ink = ink

    @slack_logfire.instrument("slack.ask_questions.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]:
        if not actions:
            return {}
        text = question_title(actions)
        message_id = await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [
                SlackOutboundMessage(
                    text=text,
                    markdown_text=text,
                    blocks=ask_question_blocks(actions),
                )
            ],
            channel_thread_id=(
                address.channel_thread_id or address.chat_id or address.user_id
            ),
        )
        return {action.id: message_id for action in actions}


def ask_question_blocks(actions: list[DeferredQuestion]) -> list[SlackBlock]:
    """Every question's inputs, then one Submit. Input blocks keep their values in
    the client until a button is pressed, so nothing is sent before Submit."""
    blocks: list[SlackBlock] = []
    for index, action in enumerate(actions, start=1):
        choices = list(action.args.get("choices") or [])[:MAX_QUESTION_CHOICES]
        hint = action.args.get("hint") or ""
        question = question_text(action)
        label = f"{index}. {question}" if len(actions) > 1 else question
        if choices:
            blocks.append(
                question_input_block(
                    block_id=question_choice_block_id(action),
                    label=label,
                    element=choice_element(
                        choices, multi=action.args.get("multi_select", False)
                    ),
                    hint=hint,
                )
            )
        input_element: SlackBlock = {
            "type": "plain_text_input",
            "action_id": SlackBlockAction.ASK_QUESTION_ANSWER.value,
            "multiline": not bool(choices),
            "placeholder": {
                "type": "plain_text",
                "text": "Optional note or other answer"
                if choices
                else "Type an answer",
            },
        }
        if choices:
            input_element["max_length"] = 160
        blocks.append(
            question_input_block(
                block_id=question_answer_block_id(action),
                label="Other" if choices else label,
                element=input_element,
                hint="" if choices else hint,
            )
        )
    blocks.append({"type": "actions", "elements": [submit_button(actions)]})
    return blocks


def question_input_block(
    *,
    block_id: str,
    label: str,
    element: SlackBlock,
    hint: object = "",
) -> SlackBlock:
    block: SlackBlock = {
        "type": "input",
        "block_id": block_id,
        "optional": True,
        "element": element,
        "label": {"type": "plain_text", "text": label},
    }
    if hint:
        block["hint"] = {"type": "plain_text", "text": str(hint)}
    return block


def submitted_blocks(
    actions: list[DeferredQuestion],
    answers: dict[UUID, QuestionAnswer] | None = None,
) -> list[SlackBlock]:
    count = len(actions)
    noun = "question" if count == 1 else "questions"
    answers = answers or {}
    summary = "\n\n".join(
        (
            f"*{index}. {question_text(action)}*\n"
            f"{answer_text(answers.get(action.id)) or '[No answer provided]'}"
        )
        for index, action in enumerate(actions, start=1)
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Answers submitted*\n_{count} {noun}_\n\n{summary}",
            },
        }
    ]


def question_choice_block_id(action: DeferredQuestion) -> str:
    return f"choice_block:{action.id}"


def question_answer_block_id(action: DeferredQuestion) -> str:
    return f"answer_block:{action.id}"


def collect_answers(
    state: SlackQuestionState,
    actions: list[DeferredQuestion],
) -> dict[UUID, QuestionAnswer]:
    """Each question's answer from the submitted state: a typed answer over a
    single pick, a multi-select question's checked boxes over typed text."""
    answers: dict[UUID, QuestionAnswer] = {}
    for action in actions:
        choice_state = state["values"].get(question_choice_block_id(action), {})
        answer_state = state["values"].get(question_answer_block_id(action), {})
        typed = answer_state.get(SlackBlockAction.ASK_QUESTION_ANSWER.value, {})
        answer = str(typed.get("value") or "").strip()
        picked = choice_state.get(SlackBlockAction.ASK_QUESTION_CHOICE.value, {})
        if action.args.get("multi_select"):
            checked = picked.get("selected_options") or []
            answers[action.id] = [str(option["value"]) for option in checked] or answer
            continue
        selected = picked.get("selected_option")
        answers[action.id] = answer or (str(selected["value"]) if selected else "")
    return answers


def choice_element(choices: list[str], *, multi: bool) -> SlackBlock:
    """A radio list, or checkboxes for a multi-select question."""
    options: list[JsonValue] = [
        {"text": {"type": "plain_text", "text": str(choice)}, "value": str(choice)}
        for choice in choices
    ]
    return {
        "type": "checkboxes" if multi else "radio_buttons",
        "action_id": SlackBlockAction.ASK_QUESTION_CHOICE.value,
        "options": options,
    }


def submit_button(actions: list[DeferredQuestion]) -> SlackBlock:
    batch_id = actions[0].batch_id
    if batch_id is None:
        raise ValueError("question buttons require a batch id")
    value: SlackQuestionActionValue = {"batch_id": batch_id, "questions": actions}
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": "Submit"},
        "action_id": SlackBlockAction.ASK_QUESTION_SUBMIT.value,
        "style": "primary",
        "value": SlackQuestionActionValueAdapter.dump_json(
            value,
            include={
                "batch_id": True,
                "questions": {"__all__": QUESTION_STATE_FIELDS},
            },
            exclude_defaults=True,
            exclude_none=True,
        ).decode(),
    }


def question_title(actions: list[DeferredQuestion]) -> str:
    count = len(actions)
    noun = "question" if count == 1 else "questions"
    return f"Octomate needs {count} {noun} answered"
