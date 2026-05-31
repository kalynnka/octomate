from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.schemas.triage import TriageDecision
from octomate.tentacles.channel.feelers import (
    ApprovalFeeler,
    AskQuestionFeeler,
    Feelers,
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.channel.lark.base import LarkTentacle
from octomate.tentacles.channel.lark.feelers import (
    LarkApprovalFeeler,
    LarkAskQuestionFeeler,
    LarkCardAction,
    LarkQuestionActionValueAdapter,
    approval_card_data,
    ask_question_card,
    ask_question_card_data,
    collect_answer,
)
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage
from octomate.tentacles.channel.slack.base import SlackTentacle
from octomate.tentacles.channel.slack.feelers import (
    SlackApprovalFeeler,
    SlackAskQuestionFeeler,
    SlackBlockAction,
    SlackQuestionActionsAdapter,
    ask_question_blocks,
    collect_current_answer,
)
from octomate.tentacles.channel.slack.schema import SlackOutboundMessage


def _key(channel: str = "im") -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id=channel,
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id="thread-1",
    )


def _question(
    *,
    batch_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
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
    batch_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
) -> DeferredApproval:
    return DeferredApproval(
        id=action_id or uuid7(),
        batch_id=batch_id or uuid7(),
        tool_name="shell",
        tool_call_id="call_approval",
        args=ApprovalRequest(tool_name="shell", args={"cmd": "git status"}),
    )


def _batch_id(action: DeferredQuestion | DeferredApproval) -> uuid.UUID:
    assert action.batch_id is not None
    return action.batch_id


async def test_plain_text_feelers_present_approval_and_questions() -> None:
    sent: list[tuple[ConversationKey, str]] = []

    async def respond_text(key: ConversationKey, text: str) -> None:
        sent.append((key, text))

    key = _key()
    approval = _approval()
    questions = [
        _question(question="Pick a deploy window", choices=["now", "later"], hint="UTC"),
        _question(question="Who approves?", position=1),
    ]

    approval_id = await PlainTextApprovalFeeler(respond_text).present(key, approval)
    question_ids = await PlainTextAskQuestionFeeler(respond_text).present(
        key,
        questions,
    )

    assert approval_id is None
    assert question_ids == {questions[0].id: None, questions[1].id: None}
    assert sent[0] == (
        key,
        (
            f"Octomate needs approval for `shell` ({approval.id}). This channel "
            "can show the request, but does not support interactive approval cards yet."
        ),
    )
    assert "Pick a deploy window" in sent[1][1]
    assert "Hint: UTC" in sent[1][1]
    assert "- now" in sent[1][1]
    assert str(questions[0].id) in sent[1][1]
    assert "Who approves?" in sent[2][1]


@dataclass
class RecordingApprovalFeeler(ApprovalFeeler):
    presented: list[tuple[ConversationKey, DeferredApproval]] = field(
        default_factory=list
    )

    async def present(
        self,
        key: ConversationKey,
        action: DeferredApproval,
    ) -> str | None:
        self.presented.append((key, action))
        return "approval-message"


@dataclass
class RecordingAskQuestionFeeler(AskQuestionFeeler):
    presented: list[tuple[ConversationKey, list[DeferredQuestion]]] = field(
        default_factory=list
    )

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[uuid.UUID, str | None]:
        self.presented.append((key, actions))
        return {action.id: f"question-{index}" for index, action in enumerate(actions)}


@dataclass
class FakeActionContext:
    questions: list[DeferredQuestion]
    approvals: list[DeferredApproval]


@dataclass
class FakeActionManager:
    context: FakeActionContext
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    presented: list[tuple[uuid.UUID, str | None]] = field(default_factory=list)

    async def create_batch(self, **kwargs: Any) -> FakeActionContext:
        self.create_calls.append(kwargs)
        return self.context

    async def mark_action_presented(
        self,
        action_id: uuid.UUID,
        platform_message_id: str | None,
    ) -> None:
        self.presented.append((action_id, platform_message_id))


async def test_feelers_present_actions_creates_batch_splits_and_marks() -> None:
    approval = _approval()
    first = _question(question="First?")
    second = _question(question="Second?", position=1)
    approvals = RecordingApprovalFeeler()
    ask_questions = RecordingAskQuestionFeeler()
    manager = FakeActionManager(FakeActionContext([first, second], [approval]))
    source_key = _key("source")
    target_key = _key("target")
    decision = TriageDecision(action="reception", reason="needs input")
    requests = DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "First?"}, {"question": "Second?"}]},
                tool_call_id="call_questions",
            )
        ],
        approvals=[
            ToolCallPart(
                tool_name="shell",
                args={"cmd": "git status"},
                tool_call_id="call_approval",
            )
        ],
    )
    conversation = Conversation(
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        channel_tentacle_id="source",
    )

    context = await Feelers(approvals, ask_questions).present_actions(
        action_manager=cast(Any, manager),
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="reception",
        source_key=source_key,
        target_key=target_key,
        target_mode="sub",
        decision=decision,
        requests=requests,
    )

    assert context is manager.context
    assert manager.create_calls[0]["conversation"] is conversation
    assert manager.create_calls[0]["source_key"] == source_key
    assert manager.create_calls[0]["target_key"] == target_key
    assert approvals.presented == [(target_key, approval)]
    assert ask_questions.presented == [(target_key, [first, second])]
    assert manager.presented == [
        (approval.id, "approval-message"),
        (first.id, "question-0"),
        (second.id, "question-1"),
    ]


@dataclass
class FakeSlackInk:
    sent: list[
        tuple[str, str, list[SlackOutboundMessage], str | None]
    ] = field(default_factory=list)
    updates: list[tuple[str, str, str, list[dict[str, Any]]]] = field(
        default_factory=list
    )
    events: list[str] | None = None

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        reply_to: str | None = None,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to))
        return f"slack-{len(self.sent)}"

    async def update_message(
        self,
        channel: str,
        message_ts: str,
        *,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        self.updates.append((channel, message_ts, text, blocks))
        if self.events is not None:
            self.events.append("update")


async def test_slack_feelers_send_approval_and_question_cards() -> None:
    ink = FakeSlackInk()
    key = _key("slack")
    approval = _approval()
    questions = [
        _question(question="Region?", choices=["us", "eu"], hint="Nearest region"),
        _question(question="Ticket?", position=1),
    ]

    approval_message_id = await SlackApprovalFeeler(cast(Any, ink)).present(
        key,
        approval,
    )
    question_message_ids = await SlackAskQuestionFeeler(cast(Any, ink)).present(
        key,
        questions,
    )

    assert approval_message_id == "slack-1"
    assert question_message_ids == {questions[0].id: "slack-2", questions[1].id: "slack-2"}
    approval_msg = ink.sent[0][2][0]
    assert approval_msg.text == "Permission required: shell"
    assert approval_msg.blocks is not None
    approval_value = json.loads(approval_msg.blocks[-1]["elements"][0]["value"])
    assert approval_msg.blocks[-1]["elements"][0]["action_id"] == (
        SlackBlockAction.APPROVAL_APPROVE.value
    )
    assert approval_value["action_id"] == str(approval.id)
    question_msg = ink.sent[1][2][0]
    assert question_msg.text == "Octomate needs 2 questions answered"
    assert question_msg.blocks is not None
    next_value = json.loads(question_msg.blocks[-1]["elements"][0]["value"])
    restored = SlackQuestionActionsAdapter.validate_python(next_value["questions"])
    assert [action.id for action in restored] == [questions[0].id, questions[1].id]


def test_slack_question_blocks_collect_answer_and_restore_state() -> None:
    actions = [
        _question(question="Color?", choices=["blue", "green"]),
        _question(question="Reason?", position=1),
    ]

    first_page = ask_question_blocks(actions)
    assert first_page[0]["elements"][0]["text"] == "Questions 1 of 2"
    assert first_page[1]["text"]["text"] == "*Color?*"
    assert first_page[-1]["elements"][0]["action_id"] == (
        SlackBlockAction.ASK_QUESTION_NEXT.value
    )
    choice_block = next(
        block for block in first_page if block.get("block_id") == "choice_block"
    )
    assert [
        option["text"]["text"] for option in choice_block["element"]["options"]
    ] == ["blue", "green"]
    assert choice_block["dispatch_action"] is True
    assert choice_block["element"]["type"] == "radio_buttons"
    input_block = next(
        block for block in first_page if block.get("block_id") == "answer_block"
    )
    assert input_block["label"]["text"] == "Other"
    assert input_block["element"]["multiline"] is False
    assert input_block["element"]["max_length"] == 160
    next_state = json.loads(first_page[-1]["elements"][0]["value"])
    restored = SlackQuestionActionsAdapter.validate_python(next_state["questions"])
    assert restored[0].args["question"] == "Color?"

    radio_answers = collect_current_answer(
        {
            "values": {
                "choice_block": {
                    SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                        "selected_option": {"value": "green"}
                    }
                }
            }
        },
        restored,
        0,
        {},
    )
    assert radio_answers == {actions[0].id: "green"}
    answers = collect_current_answer(
        {
            "values": {
                "choice_block": {
                    SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                        "selected_option": {"value": "green"}
                    }
                },
                "answer_block": {
                    SlackBlockAction.ASK_QUESTION_ANSWER.value: {
                        "value": "typed blue"
                    },
                },
            }
        },
        restored,
        0,
        {},
    )
    assert answers == {actions[0].id: "typed blue"}
    second_page = ask_question_blocks(restored, page=1, answers=answers)
    assert [button["action_id"] for button in second_page[-1]["elements"]] == [
        SlackBlockAction.ASK_QUESTION_BACK.value,
        SlackBlockAction.ASK_QUESTION_SUBMIT.value,
    ]
    restored_page = ask_question_blocks(restored, answers={actions[0].id: "green"})
    restored_choice = next(
        block for block in restored_page if block.get("block_id") == "choice_block"
    )
    assert restored_choice["element"]["initial_option"]["value"] == "green"
    only_page = ask_question_blocks([actions[0]])
    assert only_page[0]["text"]["text"] == "*Color?*"
    assert all(
        block.get("elements", [{}])[0].get("text") != "Questions 1 of 1"
        for block in only_page
    )
    assert [button["action_id"] for button in only_page[-1]["elements"]] == [
        SlackBlockAction.ASK_QUESTION_SUBMIT.value
    ]


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


async def test_slack_callbacks_emit_deferred_responses_and_update_cards() -> None:
    ink = FakeSlackInk()
    octomate = FakeOctomate()
    events: list[str] = []
    ink.events = events
    octomate.events = events
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(Any, ink)
    channel.octomate = cast(Any, octomate)
    approval = _approval()
    questions = [_question(batch_id=_batch_id(approval), question="Ship it?")]

    await channel.on_approval_action(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.APPROVAL_APPROVE.value,
                    "value": json.dumps(
                        {
                            "batch_id": str(approval.batch_id),
                            "action_id": str(approval.id),
                            "tool_name": approval.tool_name,
                            "approved": True,
                        }
                    )
                }
            ],
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "message": {"ts": "111.222"},
        },
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(approval),
        responder_id="U1",
        approvals={approval.id: True},
    )
    assert ink.updates[0][0:3] == ("C1", "111.222", "shell - Approved")

    submit_state = json.loads(
        ask_question_blocks(questions)[-1]["elements"][0]["value"]
    )
    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_SUBMIT.value,
                    "value": json.dumps(submit_state),
                }
            ],
            "state": {
                "values": {
                    "answer_block": {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {"value": "yes"}
                    }
                }
            },
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444"},
        },
    )

    assert octomate.kicks[1] == DeferredActionBatchResponse(
        batch_id=_batch_id(questions[0]),
        responder_id="U2",
        answers={questions[0].id: "yes"},
    )
    assert ink.updates[1][0:3] == ("C1", "333.444", "Answers submitted")
    assert events[-2:] == ["update", "kick"]


async def test_slack_radio_choice_submits_selected_answer() -> None:
    ink = FakeSlackInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(Any, ink)
    channel.octomate = cast(Any, octomate)
    question = _question(
        question="Ocean zone?",
        choices=["Coral Reef", "Kelp Forest"],
    )

    blocks = ask_question_blocks([question])
    submit_state = json.loads(blocks[-1]["elements"][0]["value"])
    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_SUBMIT.value,
                    "value": json.dumps(submit_state),
                }
            ],
            "state": {
                "values": {
                    "choice_block": {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Kelp Forest"}
                        }
                    }
                }
            },
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444"},
        },
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(question),
        responder_id="U2",
        answers={question.id: "Kelp Forest"},
    )
    submitted_text = ink.updates[0][3][0]["text"]["text"]
    assert "Ocean zone?" in submitted_text
    assert "Kelp Forest" in submitted_text


async def test_slack_radio_choices_preserve_answers_when_backtracking() -> None:
    ink = FakeSlackInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(Any, ink)
    channel.octomate = cast(Any, octomate)
    first = _question(
        question="Ocean zone?",
        choices=["Coral Reef", "Kelp Forest"],
    )
    second = _question(
        batch_id=_batch_id(first),
        question="Why?",
        position=1,
    )

    blocks = ask_question_blocks([first, second])
    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_CHOICE.value,
                }
            ],
            "state": {
                "values": {
                    "choice_block": {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Kelp Forest"}
                        }
                    }
                }
            },
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444", "blocks": blocks},
        },
    )

    page_block = ink.updates[0][3][0]
    assert page_block["elements"][0]["text"] == "Questions 2 of 2"
    back_button = ink.updates[0][3][-1]["elements"][0]
    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_BACK.value,
                    "value": back_button["value"],
                }
            ],
            "state": {
                "values": {
                    "answer_block": {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {
                            "value": "I like kelp"
                        }
                    }
                }
            },
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444"},
        },
    )

    choice_block = next(
        block for block in ink.updates[1][3] if block.get("block_id") == "choice_block"
    )
    assert choice_block["element"]["initial_option"]["value"] == "Kelp Forest"
    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_CHOICE.value,
                }
            ],
            "state": {
                "values": {
                    "choice_block": {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Coral Reef"}
                        }
                    }
                }
            },
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444", "blocks": ink.updates[1][3]},
        },
    )

    submit_button = next(
        button
        for button in ink.updates[2][3][-1]["elements"]
        if button["action_id"] == SlackBlockAction.ASK_QUESTION_SUBMIT.value
    )
    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_SUBMIT.value,
                    "value": submit_button["value"],
                }
            ],
            "state": {
                "values": {
                    "answer_block": {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {
                            "value": "I like reefs"
                        }
                    }
                }
            },
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444"},
        },
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(first),
        responder_id="U2",
        answers={first.id: "Coral Reef", second.id: "I like reefs"},
    )
    submitted_text = ink.updates[3][3][0]["text"]["text"]
    assert "Ocean zone?" in submitted_text
    assert "Coral Reef" in submitted_text
    assert "Why?" in submitted_text
    assert "I like reefs" in submitted_text


async def test_slack_question_submit_ignores_invalid_batch_id() -> None:
    ink = FakeSlackInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(Any, ink)
    channel.octomate = cast(Any, octomate)
    question = _question(question="Ship it?")
    submit_state = json.loads(
        ask_question_blocks([question])[-1]["elements"][0]["value"]
    )
    submit_state["batch_id"] = "not-a-uuid"

    await channel.on_question_nav(
        _ack,
        {
            "actions": [
                {
                    "action_id": SlackBlockAction.ASK_QUESTION_SUBMIT.value,
                    "value": json.dumps(submit_state),
                }
            ],
            "state": {"values": {}},
            "user": {"id": "U2"},
            "channel": {"id": "C1"},
            "message": {"ts": "333.444"},
        },
    )

    assert octomate.kicks == []
    assert ink.updates == []


@dataclass
class FakeLarkInk:
    sent: list[
        tuple[str, str, list[LarkOutboundMessage], str | None, bool]
    ] = field(default_factory=list)

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[LarkOutboundMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to, reply_in_thread))
        return f"lark-{len(self.sent)}"


async def test_lark_feelers_send_approval_and_question_cards() -> None:
    ink = FakeLarkInk()
    key = _key("lark")
    key = ConversationKey(
        channel_tentacle_id=key.channel_tentacle_id,
        chat_type=key.chat_type,
        chat_id=key.chat_id,
        user_id=key.user_id,
        thread_id="om_parent",
    )
    approval = _approval()
    questions = [_question(question="Environment?", choices=["prod", "stage"])]

    approval_message_id = await LarkApprovalFeeler(cast(Any, ink)).present(
        key,
        approval,
    )
    question_message_ids = await LarkAskQuestionFeeler(cast(Any, ink)).present(
        key,
        questions,
    )

    assert approval_message_id == "lark-1"
    assert question_message_ids == {questions[0].id: "lark-2"}
    assert ink.sent[0][3:] == ("om_parent", True)
    approval_content = json.loads(ink.sent[0][2][0].content)
    assert approval_content["elements"][2]["actions"][0]["value"]["action"] == (
        LarkCardAction.APPROVAL_APPROVE.value
    )
    question_content = json.loads(ink.sent[1][2][0].content)
    form = question_content["elements"][2]
    assert form["tag"] == "form"
    assert form["elements"][-1]["value"]["action"] == (
        LarkCardAction.ASK_QUESTION_SUBMIT.value
    )


def test_lark_card_data_and_answer_collection() -> None:
    approval = _approval()
    action = _question(question="Window?", choices=["morning", "night"], hint="UTC")

    approval_data = approval_card_data(approval)
    assert approval_data["header"]["title"]["content"] == "Permission Required"
    assert approval_data["elements"][2]["actions"][1]["value"]["action"] == (
        LarkCardAction.APPROVAL_DENY.value
    )

    card = json.loads(ask_question_card([action]))
    assert card["elements"][0]["content"].startswith("**Question 1 of 1**")
    form = card["elements"][2]
    submit = form["elements"][-1]
    submit_value = LarkQuestionActionValueAdapter.validate_python(submit["value"])
    restored = submit_value["questions"]
    assert restored[0].id == action.id
    assert restored[0].args == action.args
    answers = collect_answer(
        restored,
        submit_value["page"],
        {"choice": "night", "answer": ""},
        submit_value["answers"],
    )
    assert answers == {action.id: "night"}


async def test_lark_card_callbacks_emit_deferred_responses() -> None:
    octomate = FakeOctomate()
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.octomate = cast(Any, octomate)
    approval = _approval()
    question = _question(batch_id=_batch_id(approval), question="Proceed?")

    approval_value = approval_card_data(approval)["elements"][2]["actions"][0]["value"]
    approval_response = channel.on_card_action(
        cast(
            Any,
            SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(value=approval_value, form_value={}),
                    operator=SimpleNamespace(open_id="ou_user", user_id=""),
                )
            ),
        )
    )
    await asyncio.sleep(0)

    approval_toast = approval_response.toast
    assert approval_toast is not None
    assert approval_toast.content == "Approved"
    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(approval),
        responder_id="ou_user",
        approvals={approval.id: True},
    )

    submit_value = ask_question_card_data(actions=[question])["elements"][2][
        "elements"
    ][-1]["value"]
    submit_response = channel.on_card_action(
        cast(
            Any,
            SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(
                        value=submit_value,
                        form_value={"answer": "yes"},
                    ),
                    operator=SimpleNamespace(open_id="ou_user", user_id=""),
                )
            ),
        )
    )
    await asyncio.sleep(0)

    submit_toast = submit_response.toast
    assert submit_toast is not None
    assert submit_toast.content == "Answers submitted"
    assert octomate.kicks[1] == DeferredActionBatchResponse(
        batch_id=_batch_id(question),
        responder_id="ou_user",
        answers={question.id: "yes"},
    )
