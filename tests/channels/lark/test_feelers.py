"""Lark approval/question card feelers and card-callback tests."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import cast

from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
from pydantic import JsonValue, TypeAdapter
from uuid_utils.compat import uuid7

from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.tentacles.lark.base import LarkTentacle
from octomate.tentacles.lark.feelers.actions import LarkCardAction
from octomate.tentacles.lark.feelers.approvals import (
    LarkApprovalFeeler,
    approval_card_data,
)
from octomate.tentacles.lark.feelers.questions import (
    LarkAskQuestionFeeler,
    LarkQuestionActionValueAdapter,
    ask_question_card,
    ask_question_card_data,
    collect_answer,
    submitted_card_data,
)
from octomate.tentacles.lark.ink import LarkInk
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkCardsInk
from tests.support.channels import FakeOctomate

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


def _nav_buttons(form_elements: list[JsonObject]) -> list[JsonObject]:
    """The nav row is the trailing column_set; one button per column."""
    columns = _json_objects(form_elements[-1]["columns"])
    return [_json_objects(column["elements"])[0] for column in columns]


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


async def test_lark_feelers_send_approval_and_question_cards() -> None:
    ink = FakeLarkCardsInk()
    address = _key("lark")
    address = ChannelAddress(
        channel_tentacle_id=address.channel_tentacle_id,
        chat_type=address.chat_type,
        chat_id=address.chat_id,
        user_id=address.user_id,
        channel_thread_id="om_parent",
    )
    approval = _approval()
    questions = [_question(question="Environment?", choices=["prod", "stage"])]

    approval_message_ids = await LarkApprovalFeeler(cast(LarkInk, ink)).present(
        address,
        [approval],
    )
    question_message_ids = await LarkAskQuestionFeeler(cast(LarkInk, ink)).present(
        address,
        questions,
    )

    assert approval_message_ids == {approval.id: "lark-1"}
    assert question_message_ids == {questions[0].id: "lark-2"}
    assert ink.sent[0][3:] == (None, True, "om_parent")
    approval_content = _loaded_json_object(ink.sent[0][2][0].content)
    approval_elements = _json_objects(approval_content["elements"])
    approval_actions = _json_objects(approval_elements[2]["actions"])
    approval_value = _json_object(approval_actions[0]["value"])
    assert approval_value["action"] == (LarkCardAction.APPROVAL_APPROVE.value)
    question_content = _loaded_json_object(ink.sent[1][2][0].content)
    question_elements = _json_objects(question_content["elements"])
    form = question_elements[2]
    assert form["tag"] == "form"
    form_elements = _json_objects(form["elements"])
    nav_buttons = _nav_buttons(form_elements)
    form_value = _json_object(nav_buttons[-1]["value"])
    assert form_value["action"] == (LarkCardAction.ASK_QUESTION_SUBMIT.value)


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
    assert deny_value["action"] == (LarkCardAction.APPROVAL_DENY.value)

    card = _loaded_json_object(ask_question_card([action]))
    card_elements = _json_objects(card["elements"])
    first_content = card_elements[0]["content"]
    assert isinstance(first_content, str)
    assert first_content.startswith("**Question 1 of 1**")
    form = card_elements[2]
    form_elements = _json_objects(form["elements"])
    # Back and Next/Submit share one horizontal column_set row at the end.
    nav_buttons = _nav_buttons(form_elements)
    # Lark rejects a form with duplicate element names (ErrCode 11310), so every
    # button/input must be uniquely named.
    names = [
        str(el["name"]) for el in [*form_elements[:-1], *nav_buttons] if "name" in el
    ]
    assert len(names) == len(set(names))
    # choices render as selectable buttons (radio-like), not a dropdown
    first_choice = form_elements[0]
    assert _json_object(first_choice["text"])["content"] == "morning"
    first_choice_value = _json_object(first_choice["value"])
    assert first_choice_value["action"] == LarkCardAction.ASK_QUESTION_CHOICE.value
    assert first_choice_value["choice"] == "morning"
    submit = nav_buttons[-1]
    submit_value = LarkQuestionActionValueAdapter.validate_python(submit["value"])
    restored = submit_value["questions"]
    assert restored[0].id == action.id
    assert restored[0].args == action.args
    # the text input only overrides when something was typed; a recorded choice
    # (set when its button is clicked) is preserved through an empty input
    answers = collect_answer(
        restored,
        submit_value["page"],
        {"answer": "night"},
        submit_value["answers"],
    )
    assert answers == {action.id: "night"}
    kept = collect_answer(
        restored,
        submit_value["page"],
        {"answer": ""},
        {action.id: "morning"},
    )
    assert kept == {action.id: "morning"}


async def test_lark_card_callbacks_emit_deferred_responses() -> None:
    octomate = FakeOctomate()
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.octomate = octomate
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
    nav_buttons = _nav_buttons(form_elements)
    submit_value = _json_object(nav_buttons[-1]["value"])
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


def test_lark_submitted_card_includes_answers() -> None:
    region = _question(question="Region?")
    confirm = _question(question="Confirm?")

    data = submitted_card_data(
        [region, confirm],
        {region.id: "us-east", confirm.id: "yes"},
    )
    content = _json_objects(data["elements"])[0]["content"]
    assert isinstance(content, str)
    assert "Region?" in content
    assert "us-east" in content
    assert "Confirm?" in content
    assert "yes" in content

    missing = submitted_card_data([region], {})
    missing_content = _json_objects(missing["elements"])[0]["content"]
    assert isinstance(missing_content, str)
    assert "No answer provided" in missing_content


async def test_lark_question_choice_click_advances_to_next_question() -> None:
    octomate = FakeOctomate()
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.octomate = octomate
    first = _question(question="Q1", choices=["a", "b"])
    second = _question(batch_id=_batch_id(first), question="Q2", choices=["x", "y"])

    card = ask_question_card_data(actions=[first, second])
    form_elements = _json_objects(_json_objects(card["elements"])[2]["elements"])
    choice_value = _json_object(form_elements[0]["value"])
    assert choice_value["choice"] == "a"

    response = channel.on_card_action(
        cast(
            P2CardActionTrigger,
            SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(value=choice_value, form_value={}),
                    operator=SimpleNamespace(open_id="ou_user", user_id=""),
                )
            ),
        )
    )
    await asyncio.sleep(0)

    # Clicking a choice records it and jumps to the next question — no submit.
    assert octomate.kicks == []
    toast = response.toast
    assert toast is not None
    assert toast.content == "Received"
    assert response.card is not None
    rerendered = response.card.data
    assert rerendered is not None
    heading = _json_objects(rerendered["elements"])[0]["content"]
    assert isinstance(heading, str)
    assert "Question 2 of 2" in heading
    next_choice = _json_object(
        _json_objects(_json_objects(rerendered["elements"])[2]["elements"])[0]["value"]
    )
    assert next_choice["page"] == 1
    assert next_choice["answers"] == {str(first.id): "a"}


async def test_lark_question_choice_click_on_last_question_submits() -> None:
    octomate = FakeOctomate()
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.octomate = octomate
    question = _question(question="Pick", choices=["a", "b"])

    card = ask_question_card_data(actions=[question])
    form_elements = _json_objects(_json_objects(card["elements"])[2]["elements"])
    choice_value = _json_object(form_elements[0]["value"])
    assert choice_value["choice"] == "a"

    response = channel.on_card_action(
        cast(
            P2CardActionTrigger,
            SimpleNamespace(
                event=SimpleNamespace(
                    action=SimpleNamespace(value=choice_value, form_value={}),
                    operator=SimpleNamespace(open_id="ou_user", user_id=""),
                )
            ),
        )
    )
    await asyncio.sleep(0)

    # The only/last question: clicking a choice submits straight away.
    toast = response.toast
    assert toast is not None
    assert toast.content == "Answers submitted"
    assert octomate.kicks == [
        DeferredActionBatchResponse(
            batch_id=_batch_id(question),
            responder_id="ou_user",
            answers={question.id: "a"},
        )
    ]
