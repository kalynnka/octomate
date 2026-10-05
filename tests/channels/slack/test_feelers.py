"""Slack block-kit feelers: approval/question blocks, paging, and callbacks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import UUID7, JsonValue, TypeAdapter
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.tentacles.slack.base import SlackTentacle
from octomate.tentacles.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.slack.feelers.approvals import (
    SlackApprovalActionsAdapter,
    SlackApprovalFeeler,
    approval_blocks,
)
from octomate.tentacles.slack.feelers.questions import (
    SlackAskQuestionFeeler,
    SlackQuestionActionsAdapter,
    ask_question_blocks,
    collect_answers,
    question_answer_block_id,
    question_choice_block_id,
)
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.slack.schema import (
    SlackActionMessage,
    SlackApprovalActionBody,
    SlackApprovalActionValue,
    SlackQuestionActionBody,
    SlackQuestionActionValue,
    SlackQuestionBlockAction,
    SlackQuestionState,
)
from octomate.types.json import JsonObject
from tests.channels.slack.fakes import FakeSlackBlocksInk

JsonObjectAdapter = TypeAdapter(JsonObject)


def _key(channel: str = "im") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel,
        chat_type="thread",
        chat_id="alice",
        user_id="alice",
        channel_thread_id="thread-1",
    )


def _question(
    *,
    batch_id: UUID7 | None = None,
    action_id: UUID7 | None = None,
    position: int = 0,
    question: str = "Continue?",
    choices: list[str] | None = None,
    hint: str = "",
) -> DeferredQuestion:
    args: QuestionRequest = {"question": question}
    if choices is not None:
        args["choices"] = choices
    if hint:
        args["hint"] = hint
    return DeferredQuestion(
        id=action_id or uuid7(),
        batch_id=batch_id or uuid7(),
        tool_name="ask_questions",
        tool_call_id="call_questions",
        position=position,
        args=args,
    )


def _approval(
    *,
    batch_id: UUID7 | None = None,
    action_id: UUID7 | None = None,
) -> DeferredApproval:
    return DeferredApproval(
        id=action_id or uuid7(),
        batch_id=batch_id or uuid7(),
        tool_name="shell",
        tool_call_id="call_approval",
        args=ApprovalRequest(tool_name="shell", args={"cmd": "git status"}),
    )


def _batch_id(action: DeferredQuestion | DeferredApproval) -> UUID7:
    assert action.batch_id is not None
    return action.batch_id


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _json_objects(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    objects: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        objects.append(item)
    return objects


def _json_string(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


@dataclass
class FakeOctomate:
    kicks: list[DeferredActionBatchResponse] = field(default_factory=list)
    events: list[str] | None = None

    async def kick(self, signal: DeferredActionBatchResponse) -> None:
        self.kicks.append(signal)
        if self.events is not None:
            self.events.append("kick")


async def _ack() -> None:
    return None


def _slack_approval_body(
    *,
    action_id: str,
    value: str,
    message: SlackActionMessage | None = None,
) -> SlackApprovalActionBody:
    return {
        "actions": (
            {"action_id": action_id, "value": cast(SlackApprovalActionValue, value)},
        ),
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": message or {"ts": "111.222"},
    }


def _slack_question_body(
    *,
    action_id: str,
    state: SlackQuestionState,
    value: str,
    message: SlackActionMessage | None = None,
) -> SlackQuestionActionBody:
    action: SlackQuestionBlockAction = {
        "action_id": action_id,
        "value": cast(SlackQuestionActionValue, value),
    }
    return {
        "actions": (action,),
        "state": state,
        "user": {"id": "U2"},
        "channel": {"id": "C1"},
        "message": message or {"ts": "333.444"},
    }


async def test_slack_feelers_send_approval_and_question_blocks() -> None:
    ink = FakeSlackBlocksInk()
    address = _key("slack")
    approval = _approval()
    second_approval = _approval(batch_id=_batch_id(approval))
    questions = [
        _question(question="Region?", choices=["us", "eu"], hint="Nearest region"),
        _question(question="Ticket?", position=1),
    ]

    approval_message_ids = await SlackApprovalFeeler(cast(SlackInk, ink)).present(
        address,
        [approval, second_approval],
    )
    question_message_ids = await SlackAskQuestionFeeler(cast(SlackInk, ink)).present(
        address,
        questions,
    )

    assert approval_message_ids == {
        approval.id: "slack-1",
        second_approval.id: "slack-1",
    }
    assert question_message_ids == {
        questions[0].id: "slack-2",
        questions[1].id: "slack-2",
    }
    approval_msg = ink.sent[0][2][0]
    assert approval_msg.text == "Octomate needs 2 approvals"
    assert approval_msg.blocks is not None
    assert all(block.get("type") != "card" for block in approval_msg.blocks)
    assert all("slack_icon" not in block for block in approval_msg.blocks)
    approval_progress = _json_object(approval_msg.blocks[0]["text"])["text"]
    assert approval_progress == "*Approvals 1 of 2*"
    approval_request = _json_object(approval_msg.blocks[1]["text"])["text"]
    assert isinstance(approval_request, str)
    assert "*Permission Required: `shell`*" in approval_request
    assert "*Request:*" in approval_request
    approval_buttons = _json_objects(approval_msg.blocks[-1]["elements"])
    approval_value = _loaded_json_object(_json_string(approval_buttons[0]["value"]))
    assert approval_buttons[0]["action_id"] == (SlackBlockAction.APPROVAL_APPROVE.value)
    restored_approvals = SlackApprovalActionsAdapter.validate_python(
        approval_value["approvals"]
    )
    assert [action.id for action in restored_approvals] == [
        approval.id,
        second_approval.id,
    ]
    question_msg = ink.sent[1][2][0]
    assert question_msg.text == "Octomate needs 2 questions answered"
    assert question_msg.blocks is not None
    question_buttons = _json_objects(question_msg.blocks[-1]["elements"])
    next_value = _loaded_json_object(_json_string(question_buttons[0]["value"]))
    restored = SlackQuestionActionsAdapter.validate_python(next_value["questions"])
    assert [action.id for action in restored] == [questions[0].id, questions[1].id]


def test_slack_question_card_holds_every_question_and_one_submit() -> None:
    actions = [
        _question(question="Color?", choices=["blue", "green"]),
        _question(question="Reason?", position=1),
    ]

    blocks = ask_question_blocks(actions)

    # Every question's inputs on the one card, none of them sending on change.
    assert [
        (block["block_id"], _json_object(block["label"])["text"])
        for block in blocks
        if block["type"] == "input"
    ] == [
        (question_choice_block_id(actions[0]), "1. Color?"),
        (question_answer_block_id(actions[0]), "Other"),
        (question_answer_block_id(actions[1]), "2. Reason?"),
    ]
    assert all("dispatch_action" not in block for block in blocks)
    choice_element = _json_object(blocks[0]["element"])
    assert choice_element["type"] == "radio_buttons"
    assert [
        _json_object(option["text"])["text"]
        for option in _json_objects(choice_element["options"])
    ] == ["blue", "green"]
    other = _json_object(blocks[1]["element"])
    assert (other["multiline"], other["max_length"]) == (False, 160)
    [submit] = _json_objects(blocks[-1]["elements"])
    assert submit["action_id"] == SlackBlockAction.ASK_QUESTION_SUBMIT.value
    submit_state = _loaded_json_object(_json_string(submit["value"]))
    restored = SlackQuestionActionsAdapter.validate_python(submit_state["questions"])
    assert [action.id for action in restored] == [actions[0].id, actions[1].id]
    only = ask_question_blocks([actions[0]])
    assert _json_object(only[0]["label"])["text"] == "Color?"

    answers = collect_answers(
        {
            "values": {
                question_choice_block_id(actions[0]): {
                    SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                        "selected_option": {"value": "green"}
                    }
                },
                question_answer_block_id(actions[1]): {
                    SlackBlockAction.ASK_QUESTION_ANSWER.value: {"value": "because"},
                },
            }
        },
        restored,
    )
    assert answers == {actions[0].id: "green", actions[1].id: "because"}
    typed = collect_answers(
        {
            "values": {
                question_choice_block_id(actions[0]): {
                    SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                        "selected_option": {"value": "green"}
                    }
                },
                question_answer_block_id(actions[0]): {
                    SlackBlockAction.ASK_QUESTION_ANSWER.value: {"value": "teal"},
                },
            }
        },
        [actions[0]],
    )
    assert typed == {actions[0].id: "teal"}


async def test_slack_callbacks_emit_deferred_responses_and_update_cards() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    events: list[str] = []
    ink.events = events
    octomate.events = events
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    approval = _approval()
    questions = [_question(batch_id=_batch_id(approval), question="Ship it?")]
    approval_buttons = _json_objects(approval_blocks([approval])[-1]["elements"])
    approve_value = _json_string(approval_buttons[0]["value"])

    await channel.on_approval_action(
        _ack,
        _slack_approval_body(
            action_id=SlackBlockAction.APPROVAL_APPROVE.value,
            value=approve_value,
        ),
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(approval),
        responder_id="U1",
        approvals={approval.id: True},
    )
    assert ink.updates[0][0:3] == ("C1", "111.222", "Approvals handled")
    approval_summary = _json_object(ink.updates[0][3][0]["text"])["text"]
    assert isinstance(approval_summary, str)
    assert "shell" in approval_summary
    assert "*By:* <@U1>" in approval_summary

    question_buttons = _json_objects(ask_question_blocks(questions)[-1]["elements"])
    submit_state = _loaded_json_object(_json_string(question_buttons[0]["value"]))
    await channel.on_question_submit(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=json.dumps(submit_state),
            state={
                "values": {
                    question_answer_block_id(questions[0]): {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {"value": "yes"}
                    }
                }
            },
        ),
    )

    assert octomate.kicks[1] == DeferredActionBatchResponse(
        batch_id=_batch_id(questions[0]),
        responder_id="U2",
        answers={questions[0].id: "yes"},
    )
    assert ink.updates[1][0:3] == ("C1", "333.444", "Answers submitted")
    assert events == ["kick", "update", "kick", "update"]


@pytest.mark.parametrize("approval", [True, False], ids=["approval", "question"])
@pytest.mark.parametrize(
    "error",
    [
        ValueError("unknown deferred action batch"),
        RuntimeError("This live request is no longer awaiting a response"),
    ],
    ids=["unknown-batch", "missing-waiter"],
)
async def test_rejected_slack_response_leaves_card_unchanged(
    approval: bool,
    error: ValueError | RuntimeError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    kick = AsyncMock(side_effect=error)
    monkeypatch.setattr(octomate, "kick", kick)
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    ack = AsyncMock()

    if approval:
        buttons = _json_objects(approval_blocks([_approval()])[-1]["elements"])
        submit = channel.on_approval_action(
            ack,
            _slack_approval_body(
                action_id=SlackBlockAction.APPROVAL_APPROVE.value,
                value=_json_string(buttons[0]["value"]),
            ),
        )
    else:
        buttons = _json_objects(ask_question_blocks([_question()])[-1]["elements"])
        submit = channel.on_question_submit(
            ack,
            _slack_question_body(
                action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
                value=_json_string(buttons[0]["value"]),
                state={"values": {}},
            ),
        )
    with pytest.raises(type(error), match=str(error)):
        await submit

    ack.assert_awaited_once()
    kick.assert_awaited_once()
    assert ink.updates == []


async def test_slack_approval_blocks_advance_through_multiple_approvals() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    first = _approval()
    second = _approval(batch_id=_batch_id(first))

    approval_buttons = _json_objects(approval_blocks([first, second])[-1]["elements"])
    first_value = _json_string(approval_buttons[0]["value"])
    await channel.on_approval_action(
        _ack,
        _slack_approval_body(
            action_id=SlackBlockAction.APPROVAL_APPROVE.value,
            value=first_value,
        ),
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(first),
        responder_id="U1",
        approvals={first.id: True},
    )
    assert ink.updates[0][0:3] == ("C1", "111.222", "Octomate needs 2 approvals")
    assert _json_object(ink.updates[0][3][0]["text"])["text"] == "*Approvals 2 of 2*"

    deny_buttons = _json_objects(ink.updates[0][3][-1]["elements"])
    deny_value = _json_string(deny_buttons[1]["value"])
    await channel.on_approval_action(
        _ack,
        _slack_approval_body(
            action_id=SlackBlockAction.APPROVAL_DENY.value,
            value=deny_value,
        ),
    )

    assert octomate.kicks[1] == DeferredActionBatchResponse(
        batch_id=_batch_id(first),
        responder_id="U1",
        approvals={second.id: False},
    )
    assert ink.updates[1][0:3] == ("C1", "111.222", "Approvals handled")
    summary = _json_object(ink.updates[1][3][0]["text"])["text"]
    assert isinstance(summary, str)
    assert "Approved" in summary
    assert "Denied" in summary


async def test_slack_radio_choice_submits_selected_answer() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    question = _question(
        question="Ocean zone?",
        choices=["Coral Reef", "Kelp Forest"],
    )

    blocks = ask_question_blocks([question])
    submit_buttons = _json_objects(blocks[-1]["elements"])
    submit_state = _loaded_json_object(_json_string(submit_buttons[0]["value"]))
    await channel.on_question_submit(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=json.dumps(submit_state),
            state={
                "values": {
                    question_choice_block_id(question): {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Kelp Forest"}
                        }
                    }
                }
            },
        ),
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(question),
        responder_id="U2",
        answers={question.id: "Kelp Forest"},
    )
    submitted_text = _json_object(ink.updates[0][3][0]["text"])["text"]
    assert isinstance(submitted_text, str)
    assert "Ocean zone?" in submitted_text
    assert "Kelp Forest" in submitted_text


async def test_slack_multi_select_question_submits_its_checked_boxes() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    question = _question(
        question="Which zones?",
        choices=["Coral Reef", "Kelp Forest", "Open Ocean"],
    )
    question.args["multi_select"] = True

    blocks = ask_question_blocks([question])
    choice_block = blocks[0]
    assert "dispatch_action" not in choice_block
    assert _json_object(choice_block["element"])["type"] == "checkboxes"
    submit_buttons = _json_objects(blocks[-1]["elements"])
    submit_state = _loaded_json_object(_json_string(submit_buttons[0]["value"]))
    await channel.on_question_submit(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=json.dumps(submit_state),
            state={
                "values": {
                    question_choice_block_id(question): {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_options": [
                                {"value": "Coral Reef"},
                                {"value": "Open Ocean"},
                            ]
                        }
                    }
                }
            },
        ),
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(question),
        responder_id="U2",
        answers={question.id: ["Coral Reef", "Open Ocean"]},
    )
    submitted_text = _json_object(ink.updates[0][3][0]["text"])["text"]
    assert isinstance(submitted_text, str)
    assert "Coral Reef, Open Ocean" in submitted_text


async def test_slack_submitted_question_summary_strips_stale_progress_suffix() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    question = _question(
        question=(
            "If you could have any animal as a sidekick, which one would it be? "
            "(Question 3 of 3)"
        ),
        choices=["A mysterious cat :cat2:"],
    )

    blocks = ask_question_blocks([question])
    submit_buttons = _json_objects(blocks[-1]["elements"])
    submit_state = _loaded_json_object(_json_string(submit_buttons[0]["value"]))
    await channel.on_question_submit(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=json.dumps(submit_state),
            state={
                "values": {
                    question_choice_block_id(question): {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "A mysterious cat :cat2:"}
                        }
                    }
                }
            },
        ),
    )

    submitted_text = _json_object(ink.updates[0][3][0]["text"])["text"]
    assert isinstance(submitted_text, str)
    assert "1 question" in submitted_text
    assert "Question 3 of 3" not in submitted_text
    assert "A mysterious cat :cat2:" in submitted_text


async def test_slack_submit_reads_every_question_at_once() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    first = _question(
        question="Ocean zone?",
        choices=["Coral Reef", "Kelp Forest"],
    )
    second = _question(
        batch_id=_batch_id(first),
        question="Why?",
        position=1,
    )

    [submit_button] = _json_objects(
        ask_question_blocks([first, second])[-1]["elements"]
    )
    await channel.on_question_submit(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=_json_string(submit_button["value"]),
            state={
                "values": {
                    question_choice_block_id(first): {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Coral Reef"}
                        }
                    },
                    question_answer_block_id(second): {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {
                            "value": "I like reefs"
                        }
                    },
                }
            },
        ),
    )

    assert octomate.kicks == [
        DeferredActionBatchResponse(
            batch_id=_batch_id(first),
            responder_id="U2",
            answers={first.id: "Coral Reef", second.id: "I like reefs"},
        )
    ]
    [(_, _, _, blocks)] = ink.updates
    submitted_text = _json_object(blocks[0]["text"])["text"]
    assert isinstance(submitted_text, str)
    assert "Ocean zone?" in submitted_text
    assert "Coral Reef" in submitted_text
    assert "Why?" in submitted_text
    assert "I like reefs" in submitted_text


async def test_slack_question_submit_ignores_invalid_batch_id() -> None:
    ink = FakeSlackBlocksInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    question = _question(question="Ship it?")
    submit_buttons = _json_objects(ask_question_blocks([question])[-1]["elements"])
    submit_state = _loaded_json_object(_json_string(submit_buttons[0]["value"]))
    submit_state["batch_id"] = "not-a-uuid"

    await channel.on_question_submit(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=json.dumps(submit_state),
            state={"values": {}},
        ),
    )

    assert octomate.kicks == []
    assert ink.updates == []
