from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from pydantic import JsonValue, TypeAdapter
from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate import Octomate
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    ApprovalFeeler,
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
    QuestionFeeler,
)
from octomate.tentacles.channel.lark.base import LarkTentacle
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.feelers.approvals import (
    LarkApprovalFeeler,
    approval_card_data,
)
from octomate.tentacles.channel.lark.feelers.questions import (
    LarkAskQuestionFeeler,
    LarkQuestionActionValueAdapter,
    ask_question_card,
    ask_question_card_data,
    collect_answer,
)
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.slack.base import SlackTentacle
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.approvals import (
    SlackApprovalActionsAdapter,
    SlackApprovalFeeler,
    approval_blocks,
)
from octomate.tentacles.channel.slack.feelers.questions import (
    SlackAskQuestionFeeler,
    SlackQuestionActionsAdapter,
    ask_question_blocks,
    collect_current_answer,
    question_answer_block_id,
    question_choice_block_id,
)
from octomate.tentacles.channel.slack.schema import (
    SlackActionMessage,
    SlackApprovalActionBody,
    SlackApprovalActionValue,
    SlackOutboundMessage,
    SlackQuestionActionBody,
    SlackQuestionActionValue,
    SlackQuestionBlockAction,
    SlackQuestionState,
    SlackBlock,
)
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.types.json import JsonObject

JsonObjectAdapter = TypeAdapter(JsonObject)


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


async def test_plain_text_feelers_present_approval_and_questions() -> None:
    sent: list[tuple[ConversationKey, str]] = []

    @dataclass
    class MarkdownRecorder:
        async def present(
            self,
            key: ConversationKey,
            markdown: str,
        ) -> str | None:
            sent.append((key, markdown))
            return None

    key = _key()
    markdown = MarkdownRecorder()
    approval = _approval()
    questions = [
        _question(
            question="Pick a deploy window", choices=["now", "later"], hint="UTC"
        ),
        _question(question="Who approves?", position=1),
    ]

    approval_ids = await PlainTextApprovalFeeler(markdown).present(
        key,
        [approval],
    )
    question_ids = await PlainTextAskQuestionFeeler(markdown).present(
        key,
        questions,
    )

    assert approval_ids == {approval.id: None}
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
    presented: list[tuple[ConversationKey, list[DeferredApproval]]] = field(
        default_factory=list
    )

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[uuid.UUID, str | None]:
        self.presented.append((key, actions))
        return {action.id: "approval-message" for action in actions}


@dataclass
class RecordingQuestionFeeler(QuestionFeeler):
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
class RecordingMarkdownFeeler:
    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> str | None:
        return None


class NoopMarkdownStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, str],
    ) -> str | None:
        return None


class NoopEventStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[str]],
    ) -> str | None:
        return None


@dataclass
class FakeActionContext:
    questions: list[DeferredQuestion]
    approvals: list[DeferredApproval]


@dataclass(frozen=True)
class CreateBatchCall:
    conversation: Conversation
    agent_tentacle_id: str
    run_name: str | None
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode
    decision: TriageDecision | None
    requests: DeferredToolRequests


@dataclass
class FakeActionManager:
    context: FakeActionContext
    create_calls: list[CreateBatchCall] = field(default_factory=list)
    presented: list[tuple[uuid.UUID, str | None]] = field(default_factory=list)

    async def create_batch(
        self,
        *,
        conversation: Conversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> FakeActionContext:
        self.create_calls.append(
            CreateBatchCall(
                conversation=conversation,
                agent_tentacle_id=agent_tentacle_id,
                run_name=run_name,
                source_key=source_key,
                target_key=target_key,
                target_mode=target_mode,
                decision=decision,
                requests=requests,
            )
        )
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
    ask_questions = RecordingQuestionFeeler()
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

    context = await Feelers(
        markdown=RecordingMarkdownFeeler(),
        markdown_stream=NoopMarkdownStreamFeeler(),
        event_stream=NoopEventStreamFeeler(),
        approvals=approvals,
        ask_questions=ask_questions,
    ).present_actions(
        action_manager=cast(DeferredActionManager, manager),
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
    assert manager.create_calls[0].conversation is conversation
    assert manager.create_calls[0].source_key == source_key
    assert manager.create_calls[0].target_key == target_key
    assert approvals.presented == [(target_key, [approval])]
    assert ask_questions.presented == [(target_key, [first, second])]
    assert manager.presented == [
        (approval.id, "approval-message"),
        (first.id, "question-0"),
        (second.id, "question-1"),
    ]


@dataclass
class FakeSlackInk:
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )
    updates: list[tuple[str, str, str, list[SlackBlock]]] = field(
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
        blocks: list[SlackBlock],
    ) -> None:
        self.updates.append((channel, message_ts, text, blocks))
        if self.events is not None:
            self.events.append("update")


async def test_slack_feelers_send_approval_and_question_blocks() -> None:
    ink = FakeSlackInk()
    key = _key("slack")
    approval = _approval()
    second_approval = _approval(batch_id=_batch_id(approval))
    questions = [
        _question(question="Region?", choices=["us", "eu"], hint="Nearest region"),
        _question(question="Ticket?", position=1),
    ]

    approval_message_ids = await SlackApprovalFeeler(cast(SlackInk, ink)).present(
        key,
        [approval, second_approval],
    )
    question_message_ids = await SlackAskQuestionFeeler(cast(SlackInk, ink)).present(
        key,
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
    assert approval_buttons[0]["action_id"] == (
        SlackBlockAction.APPROVAL_APPROVE.value
    )
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


def test_slack_question_blocks_collect_answer_and_restore_state() -> None:
    actions = [
        _question(question="Color?", choices=["blue", "green"]),
        _question(question="Reason?", position=1),
    ]

    first_page = ask_question_blocks(actions)
    assert first_page[0]["type"] == "section"
    assert _json_object(first_page[0]["text"])["text"] == "*Questions 1 of 2*"
    first_nav = _json_objects(first_page[-1]["elements"])
    assert first_nav[0]["action_id"] == (
        SlackBlockAction.ASK_QUESTION_NEXT.value
    )
    choice_block = next(
        block
        for block in first_page
        if block.get("block_id") == question_choice_block_id(actions[0])
    )
    assert _json_object(choice_block["label"])["text"] == "Color?"
    choice_element = _json_object(choice_block["element"])
    choice_options = _json_objects(choice_element["options"])
    assert [
        _json_object(option["text"])["text"] for option in choice_options
    ] == ["blue", "green"]
    assert choice_block["dispatch_action"] is True
    assert choice_element["type"] == "radio_buttons"
    input_block = next(
        block
        for block in first_page
        if block.get("block_id") == question_answer_block_id(actions[0])
    )
    assert _json_object(input_block["label"])["text"] == "Other"
    input_element = _json_object(input_block["element"])
    assert input_element["multiline"] is False
    assert input_element["max_length"] == 160
    next_state = _loaded_json_object(_json_string(first_nav[0]["value"]))
    restored = SlackQuestionActionsAdapter.validate_python(next_state["questions"])
    assert restored[0].args["question"] == "Color?"

    radio_answers = collect_current_answer(
        {
            "values": {
                question_choice_block_id(actions[0]): {
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
                question_choice_block_id(actions[0]): {
                    SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                        "selected_option": {"value": "green"}
                    }
                },
                question_answer_block_id(actions[0]): {
                    SlackBlockAction.ASK_QUESTION_ANSWER.value: {"value": "typed blue"},
                },
            }
        },
        restored,
        0,
        {},
    )
    assert answers == {actions[0].id: "typed blue"}
    second_page = ask_question_blocks(restored, page=1, answers=answers)
    assert _json_object(second_page[0]["text"])["text"] == "*Questions 2 of 2*"
    second_nav = _json_objects(second_page[-1]["elements"])
    assert [button["action_id"] for button in second_nav] == [
        SlackBlockAction.ASK_QUESTION_BACK.value,
        SlackBlockAction.ASK_QUESTION_SUBMIT.value,
    ]
    restored_page = ask_question_blocks(restored, answers={actions[0].id: "green"})
    restored_choice = next(
        block
        for block in restored_page
        if block.get("block_id") == question_choice_block_id(actions[0])
    )
    restored_element = _json_object(restored_choice["element"])
    assert _json_object(restored_element["initial_option"])["value"] == "green"
    only_page = ask_question_blocks([actions[0]])
    assert _json_object(only_page[0]["label"])["text"] == "Color?"
    only_nav = _json_objects(only_page[-1]["elements"])
    assert [button["action_id"] for button in only_nav] == [
        SlackBlockAction.ASK_QUESTION_SUBMIT.value
    ]


def test_slack_question_pages_use_distinct_input_blocks() -> None:
    first = _question(question="Snack?", choices=["chips", "cookies"])
    second = _question(
        batch_id=_batch_id(first),
        question="Ocean mood?",
        choices=["Calm Coral Reef", "Exciting Open Ocean"],
        position=1,
    )

    first_page = ask_question_blocks([first, second], answers={first.id: "chips"})
    second_page = ask_question_blocks(
        [first, second],
        page=1,
        answers={first.id: "chips"},
    )

    assert {
        block.get("block_id")
        for block in first_page[:-1]
        if block.get("type") == "input"
    } == {
        question_choice_block_id(first),
        question_answer_block_id(first),
    }
    assert {
        block.get("block_id")
        for block in second_page[:-1]
        if block.get("type") == "input"
    } == {
        question_choice_block_id(second),
        question_answer_block_id(second),
    }
    assert all(block.get("type") != "card" for block in first_page)
    assert all("slack_icon" not in block for block in first_page)
    second_choice = next(
        block
        for block in second_page
        if block.get("block_id") == question_choice_block_id(second)
    )
    second_answer = next(
        block
        for block in second_page
        if block.get("block_id") == question_answer_block_id(second)
    )
    second_label = _json_object(second_choice["label"])["text"]
    assert _json_object(second_page[0]["text"])["text"] == "*Questions 2 of 2*"
    assert second_label == "Ocean mood?"
    second_options = _json_objects(_json_object(second_choice["element"])["options"])
    assert [
        _json_object(option["text"])["text"] for option in second_options
    ] == ["Calm Coral Reef", "Exciting Open Ocean"]
    assert "initial_option" not in _json_object(second_choice["element"])
    assert "initial_value" not in _json_object(second_answer["element"])


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
    value: str | None = None,
    message: SlackActionMessage | None = None,
) -> SlackQuestionActionBody:
    action: SlackQuestionBlockAction = {"action_id": action_id}
    if value is not None:
        action["value"] = cast(SlackQuestionActionValue, value)
    return {
        "actions": (action,),
        "state": state,
        "user": {"id": "U2"},
        "channel": {"id": "C1"},
        "message": message or {"ts": "333.444"},
    }


async def test_slack_callbacks_emit_deferred_responses_and_update_cards() -> None:
    ink = FakeSlackInk()
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
    await channel.on_question_nav(
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
    assert events[-2:] == ["update", "kick"]


async def test_slack_approval_blocks_advance_through_multiple_approvals() -> None:
    ink = FakeSlackInk()
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
    assert (
        _json_object(ink.updates[0][3][0]["text"])["text"]
        == "*Approvals 2 of 2*"
    )

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
    ink = FakeSlackInk()
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
    await channel.on_question_nav(
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


async def test_slack_radio_choices_preserve_answers_when_backtracking() -> None:
    ink = FakeSlackInk()
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

    blocks = ask_question_blocks([first, second])
    await channel.on_question_nav(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_CHOICE.value,
            state={
                "values": {
                    question_choice_block_id(first): {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Kelp Forest"}
                        }
                    }
                }
            },
            message={"ts": "333.444", "blocks": blocks},
        ),
    )

    page_block = ink.updates[0][3][0]
    assert _json_object(page_block["text"])["text"] == "*Questions 2 of 2*"
    page_answer = next(
        block
        for block in ink.updates[0][3]
        if block.get("block_id") == question_answer_block_id(second)
    )
    assert _json_object(page_answer["label"])["text"] == "Why?"
    page_buttons = _json_objects(ink.updates[0][3][-1]["elements"])
    back_button = page_buttons[0]
    await channel.on_question_nav(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_BACK.value,
            value=_json_string(back_button["value"]),
            state={
                "values": {
                    question_answer_block_id(second): {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {
                            "value": "I like kelp"
                        }
                    }
                }
            },
        ),
    )

    choice_block = next(
        block
        for block in ink.updates[1][3]
        if block.get("block_id") == question_choice_block_id(first)
    )
    choice_element = _json_object(choice_block["element"])
    assert _json_object(choice_element["initial_option"])["value"] == "Kelp Forest"
    await channel.on_question_nav(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_CHOICE.value,
            state={
                "values": {
                    question_choice_block_id(first): {
                        SlackBlockAction.ASK_QUESTION_CHOICE.value: {
                            "selected_option": {"value": "Coral Reef"}
                        }
                    }
                }
            },
            message={"ts": "333.444", "blocks": ink.updates[1][3]},
        ),
    )

    submit_button = next(
        button
        for button in _json_objects(ink.updates[2][3][-1]["elements"])
        if button["action_id"] == SlackBlockAction.ASK_QUESTION_SUBMIT.value
    )
    await channel.on_question_nav(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=_json_string(submit_button["value"]),
            state={
                "values": {
                    question_answer_block_id(second): {
                        SlackBlockAction.ASK_QUESTION_ANSWER.value: {
                            "value": "I like reefs"
                        }
                    }
                }
            },
        ),
    )

    assert octomate.kicks[0] == DeferredActionBatchResponse(
        batch_id=_batch_id(first),
        responder_id="U2",
        answers={first.id: "Coral Reef", second.id: "I like reefs"},
    )
    submitted_text = _json_object(ink.updates[3][3][0]["text"])["text"]
    assert isinstance(submitted_text, str)
    assert "Ocean zone?" in submitted_text
    assert "Coral Reef" in submitted_text
    assert "Why?" in submitted_text
    assert "I like reefs" in submitted_text


async def test_slack_question_submit_ignores_invalid_batch_id() -> None:
    ink = FakeSlackInk()
    octomate = FakeOctomate()
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInk, ink)
    channel.octomate = cast(Octomate, octomate)
    question = _question(question="Ship it?")
    submit_buttons = _json_objects(ask_question_blocks([question])[-1]["elements"])
    submit_state = _loaded_json_object(_json_string(submit_buttons[0]["value"]))
    submit_state["batch_id"] = "not-a-uuid"

    await channel.on_question_nav(
        _ack,
        _slack_question_body(
            action_id=SlackBlockAction.ASK_QUESTION_SUBMIT.value,
            value=json.dumps(submit_state),
            state={"values": {}},
        ),
    )

    assert octomate.kicks == []
    assert ink.updates == []


@dataclass
class FakeLarkInk:
    sent: list[tuple[str, str, list[LarkOutboundMessage], str | None, bool]] = field(
        default_factory=list
    )

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

    approval_message_ids = await LarkApprovalFeeler(cast(LarkInk, ink)).present(
        key,
        [approval],
    )
    question_message_ids = await LarkAskQuestionFeeler(cast(LarkInk, ink)).present(
        key,
        questions,
    )

    assert approval_message_ids == {approval.id: "lark-1"}
    assert question_message_ids == {questions[0].id: "lark-2"}
    assert ink.sent[0][3:] == ("om_parent", True)
    approval_content = _loaded_json_object(ink.sent[0][2][0].content)
    approval_elements = _json_objects(approval_content["elements"])
    approval_actions = _json_objects(approval_elements[2]["actions"])
    approval_value = _json_object(approval_actions[0]["value"])
    assert approval_value["action"] == (
        LarkCardAction.APPROVAL_APPROVE.value
    )
    question_content = _loaded_json_object(ink.sent[1][2][0].content)
    question_elements = _json_objects(question_content["elements"])
    form = question_elements[2]
    assert form["tag"] == "form"
    form_elements = _json_objects(form["elements"])
    form_value = _json_object(form_elements[-1]["value"])
    assert form_value["action"] == (
        LarkCardAction.ASK_QUESTION_SUBMIT.value
    )


def test_lark_card_data_and_answer_collection() -> None:
    approval = _approval()
    action = _question(question="Window?", choices=["morning", "night"], hint="UTC")

    approval_data = approval_card_data(approval)
    approval_header = _json_object(approval_data["header"])
    approval_title = _json_object(approval_header["title"])
    assert approval_title["content"] == "Permission Required"
    approval_elements = _json_objects(approval_data["elements"])
    approval_actions = _json_objects(approval_elements[2]["actions"])
    deny_value = _json_object(approval_actions[1]["value"])
    assert deny_value["action"] == (
        LarkCardAction.APPROVAL_DENY.value
    )

    card = _loaded_json_object(ask_question_card([action]))
    card_elements = _json_objects(card["elements"])
    first_content = card_elements[0]["content"]
    assert isinstance(first_content, str)
    assert first_content.startswith("**Question 1 of 1**")
    form = card_elements[2]
    form_elements = _json_objects(form["elements"])
    submit = form_elements[-1]
    submit_value = LarkQuestionActionValueAdapter.validate_python(
        submit["value"]
    )
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
    channel.octomate = cast(Octomate, octomate)
    approval = _approval()
    question = _question(batch_id=_batch_id(approval), question="Proceed?")

    approval_data = approval_card_data(approval)
    approval_elements = _json_objects(approval_data["elements"])
    approval_actions = _json_objects(approval_elements[2]["actions"])
    approval_value = _json_object(approval_actions[0]["value"])
    approval_response = channel.on_card_action(
        cast(
            P2CardActionTrigger,
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

    question_data = ask_question_card_data(actions=[question])
    question_elements = _json_objects(question_data["elements"])
    form_elements = _json_objects(question_elements[2]["elements"])
    submit_value = _json_object(form_elements[-1]["value"])
    submit_response = channel.on_card_action(
        cast(
            P2CardActionTrigger,
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
