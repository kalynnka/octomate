"""ACP callbacks preserve form values, provider choices, and live reply lifetimes."""

import asyncio
import json

import pytest
from acp import schema
from anyio import Path as AsyncPath
from pydantic import SecretStr, TypeAdapter

from octomate.capabilities.harness.events import (
    ActionBatchAnsweredEvent,
    ActionBatchEvent,
    ActionBatchUpdatedEvent,
)
from octomate.nerve import ChannelTopic
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.deferred import QuestionRequest
from octomate.schemas.thread import Thread
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.base import AcpAgentTentacle
from octomate.tentacles.acp.elicitation import AcpForm, FormProperty
from octomate.types.json import JsonObject
from tests.agent.test_acp_client import adapter_environment as adapter_environment
from tests.agent.test_acp_tentacle import agent as agent


def test_form_presentation_state_is_not_advertised_to_question_tools() -> None:
    properties = TypeAdapter(QuestionRequest).json_schema()["properties"]
    assert "editable" not in properties
    assert "error" not in properties


@pytest.fixture
def suspender(agent: tuple[AcpAgentTentacle, UserProfile, Thread]) -> ReflexSuspender:
    tentacle, sender, thread = agent
    address = thread.key.address(sender.channel_user_id)
    return ReflexSuspender(
        action_manager=tentacle.octomate.deferred_actions,
        conversation_manager=tentacle.octomate.conversations,
        agent_tentacle_id=tentacle.id,
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="sub",
        decision=None,
        thread_id=thread.id,
    )


async def test_form_can_correct_invalid_answers_and_revise_drafts_before_submit(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    suspender: ReflexSuspender,
) -> None:
    tentacle, _, _ = agent
    updates = asyncio.Queue()
    request = schema.CreateFormSessionElicitationRequest.model_validate(
        {
            "sessionId": "native-1",
            "mode": "form",
            "message": "Choose settings",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "note": {"type": "string"},
                },
                "required": ["count"],
            },
        }
    )
    task = asyncio.create_task(tentacle.elicit(request, suspender, updates))
    async with asyncio.timeout(5):
        event = await updates.get()
        assert isinstance(event, ActionBatchEvent)
        count, note, control = event.questions
        assert count.batch_id is not None
        manager = tentacle.octomate.deferred_actions
        topic = ChannelTopic(suspender.target_address.channel_tentacle_id)
        with tentacle.octomate.nerve.subscribe(topic) as changes:
            await tentacle.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=count.batch_id,
                    answers={count.id: "0", note.id: "keep this", control.id: "Submit"},
                )
            )
            update = (await changes.receive()).event
            assert isinstance(update, ActionBatchUpdatedEvent)
            assert not update.closed
            assert update.questions[0].args.get("error")
            assert update.questions[-1].result is None
            assert not task.done()

            # A corrected field alone does not implicitly resubmit the form.
            await tentacle.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=count.batch_id, answers={count.id: "2"}
                )
            )
            update = (await changes.receive()).event
            assert isinstance(update, ActionBatchUpdatedEvent)
            assert not update.closed
            saved = await manager.get_batch(count.batch_id)
            assert saved.status == "pending"
            assert saved.questions[0].result == "2"
            assert "error" not in saved.questions[0].args
            assert not task.done()

            await tentacle.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=count.batch_id,
                    answers={count.id: "3", control.id: "Submit"},
                )
            )
            response = await task
            assert isinstance(response, schema.AcceptElicitationResponse)
            assert response.content == {"count": 3, "note": "keep this"}
            update = (await changes.receive()).event
            assert isinstance(update, ActionBatchUpdatedEvent)
            assert update.closed
            assert all(action.status == "answered" for action in update.questions)
    assert not tentacle.pendings


async def test_form_roundtrip_preserves_typed_answers(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    suspender: ReflexSuspender,
    adapter_environment: dict[str, str],
) -> None:
    tentacle, sender, thread = agent
    tentacle.provider.env.update(
        {
            "ACP_TEST_CALLBACK": SecretStr("elicitation/create"),
            "ACP_TEST_FORM": SecretStr(
                json.dumps(
                    {
                        "mode": "form",
                        "message": "Choose settings",
                        "toolCallId": "settings",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {
                                "theme": {
                                    "type": "string",
                                    "title": "Theme",
                                    "oneOf": [{"const": "dark", "title": "Dark mode"}],
                                },
                                "count": {
                                    "type": "integer",
                                    "title": "Count",
                                    "minimum": 1,
                                },
                                "note": {"type": "string", "title": "Note"},
                            },
                            "required": ["theme", "count"],
                        },
                    }
                )
            ),
        }
    )
    answers = ["Dark mode", "3", "", "Submit"]
    batch: ActionBatchEvent | None = None
    answered = 0
    async with tentacle.run_stream_events(
        "hello",
        conversation_address=thread.key.address(sender.channel_user_id),
        thread_id=thread.id,
        deferred_suspender=suspender,
    ) as events:
        async for event in events:
            if isinstance(event, ActionBatchEvent):
                batch = event
                assert len(batch.questions) == 4
                assert batch.questions[0].args.get("choices") == ["Dark mode"]
            elif isinstance(event, ActionBatchAnsweredEvent):
                answered += 1
            else:
                continue
            if answered:
                continue
            assert batch is not None
            assert batch.questions[0].batch_id is not None
            await tentacle.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=batch.questions[0].batch_id,
                    answers={
                        question.id: answer
                        for question, answer in zip(
                            batch.questions, answers, strict=True
                        )
                    },
                )
            )
    assert answered == 0
    assert not tentacle.pendings
    messages = [
        json.loads(line)["message"]
        for line in (
            await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
        ).splitlines()
    ]
    assert messages[0]["params"]["clientCapabilities"]["elicitation"] == {"form": {}}
    reply = next(message for message in messages if message.get("id") == "permission")
    assert reply["result"] == {
        "action": "accept",
        "content": {"theme": "dark", "count": 3},
    }


@pytest.mark.parametrize(
    "kind",
    ["allow_once", "allow_always", "reject_once", "reject_always", "second_once"],
)
async def test_permission_returns_exact_selected_option(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    suspender: ReflexSuspender,
    adapter_environment: dict[str, str],
    kind: str,
) -> None:
    tentacle, sender, thread = agent
    options = [
        {"optionId": kind, "name": name, "kind": kind}
        for kind, name in [
            ("allow_once", "Allow once"),
            ("allow_always", "Allow for this directory"),
            ("reject_once", "Reject once"),
            ("reject_always", "Always reject"),
        ]
    ]
    options.append(
        {
            "optionId": "second_once",
            "name": "Allow edited command",
            "kind": "allow_once",
        }
    )
    tentacle.provider.env["ACP_TEST_OPTIONS"] = SecretStr(json.dumps(options))
    async with tentacle.run_stream_events(
        "hello",
        conversation_address=thread.key.address(sender.channel_user_id),
        thread_id=thread.id,
        deferred_suspender=suspender,
    ) as events:
        async for event in events:
            if not isinstance(event, ActionBatchEvent):
                continue
            [question] = event.questions
            assert question.batch_id is not None
            chosen = next(option for option in options if option["optionId"] == kind)
            await tentacle.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=question.batch_id,
                    answers={question.id: chosen["name"]},
                )
            )
    messages = [
        json.loads(line)["message"]
        for line in (
            await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
        ).splitlines()
    ]
    reply = next(message for message in messages if message.get("id") == "permission")
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": kind}


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("form", [False, True])
async def test_unanswered_callback_expires_and_removes_waiter(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    suspender: ReflexSuspender,
    cancel: bool,
    form: bool,
) -> None:
    tentacle, _, _ = agent
    tentacle.approval_timeout = None if cancel else 0.01
    updates = asyncio.Queue()
    task = asyncio.create_task(
        tentacle.elicit(
            schema.CreateFormSessionElicitationRequest.model_validate(
                {
                    "sessionId": "native-1",
                    "mode": "form",
                    "message": "Question",
                    "requestedSchema": {"properties": {"field": {"type": "string"}}},
                }
            ),
            suspender,
            updates,
        )
        if form
        else tentacle.permission(
            schema.RequestPermissionRequest.model_validate(
                {
                    "sessionId": "native-1",
                    "toolCall": {"toolCallId": "edit", "title": "Edit"},
                    "options": [
                        {"optionId": "yes", "name": "Allow", "kind": "allow_once"}
                    ],
                }
            ),
            suspender,
            updates,
        )
    )
    event = await updates.get()
    assert isinstance(event, ActionBatchEvent)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        if isinstance(result, schema.RequestPermissionResponse):
            assert result.outcome.outcome == "cancelled"
        else:
            assert result.action == "cancel"
    assert not tentacle.pendings
    batch_id = (event.questions if form else event.approvals)[0].batch_id
    assert batch_id is not None
    assert (
        await tentacle.octomate.deferred_actions.get_batch(batch_id)
    ).status == "expired"
    with pytest.raises(RuntimeError, match="no longer awaiting"):
        await tentacle.octomate.kick(DeferredActionBatchResponse(batch_id=batch_id))


async def test_modes_reach_new_and_loaded_provider_sessions(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    adapter_environment: dict[str, str],
) -> None:
    tentacle, sender, thread = agent
    tentacle.configured_permission_mode = "plan"
    address = thread.key.address(sender.channel_user_id)
    async with tentacle:
        assert tentacle.default_permission_mode == "plan"
        assert {mode.value for mode in tentacle.permission_modes} == {"default", "plan"}
        for method in ["session/new", "session/load"]:
            await tentacle.run(
                "hello",
                conversation_address=address,
                thread_id=thread.id,
                interactive=False,
            )
            messages = [
                json.loads(line)["message"]
                for line in (
                    await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
                ).splitlines()
            ]
            assert messages[1]["method"] == method
            mode = next(
                message
                for message in messages
                if message.get("method") == "session/set_config_option"
            )
            assert mode["params"]["configId"] == "mode"
            assert mode["params"]["value"] == "plan"
        conversation = await tentacle.resolve_conversation(
            thread.id, conversation_address=address
        )
        await tentacle.set_permission_mode(conversation, "default")
        await tentacle.run(
            "hello",
            conversation_address=address,
            thread_id=thread.id,
            interactive=False,
        )
        messages = [
            json.loads(line)["message"]
            for line in (
                await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
            ).splitlines()
        ]
        assert not any(
            message.get("method") == "session/set_config_option" for message in messages
        )


@pytest.mark.parametrize(
    ("decision", "action"), [("Decline", "decline"), ("Cancel", "cancel")]
)
async def test_form_can_close_without_answering_required_fields(
    agent: tuple[AcpAgentTentacle, UserProfile, Thread],
    suspender: ReflexSuspender,
    decision: str,
    action: str,
) -> None:
    tentacle, _, _ = agent
    updates = asyncio.Queue()
    task = asyncio.create_task(
        tentacle.elicit(
            schema.CreateFormSessionElicitationRequest.model_validate(
                {
                    "sessionId": "native-1",
                    "mode": "form",
                    "message": "Question",
                    "requestedSchema": {
                        "properties": {"field": {"type": "string"}},
                        "required": ["field"],
                    },
                }
            ),
            suspender,
            updates,
        )
    )
    event = await updates.get()
    assert isinstance(event, ActionBatchEvent)
    control = event.questions[-1]
    assert control.batch_id is not None
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(
            batch_id=control.batch_id, answers={control.id: decision}
        )
    )
    response = await task
    assert response.action == action
    assert response.model_dump(exclude_none=True) == {"action": action}
    assert not tentacle.pendings


@pytest.mark.parametrize(
    ("prop", "answer", "expected"),
    [
        (schema.ElicitationBooleanPropertySchema(type="boolean"), "Yes", True),
        (schema.ElicitationNumberPropertySchema(type="number", maximum=2), "1.5", 1.5),
        (schema.ElicitationIntegerPropertySchema(type="integer"), "3", 3),
        (
            schema.ElicitationStringPropertySchema(type="string", enum=["a", "b"]),
            "b",
            "b",
        ),
        (
            schema.ElicitationMultiSelectPropertySchema(
                type="array",
                items=schema.UntitledMultiSelectItems(type="string", enum=["a", "b"]),
            ),
            ["a", "b"],
            ["a", "b"],
        ),
    ],
)
def test_form_preserves_wire_types(
    prop: FormProperty,
    answer: str | list[str],
    expected: str | int | float | bool | list[str],
) -> None:
    value = AcpForm.value(prop, answer)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize(
    ("prop", "answer"),
    [
        ({"type": "integer", "minimum": 1}, "0"),
        ({"type": "integer"}, "1.5"),
        ({"type": "number"}, "nan"),
        ({"type": "boolean"}, "perhaps"),
        ({"type": "string", "enum": ["a"]}, "b"),
        ({"type": "array", "items": {"type": "string", "enum": ["a"]}}, ["b"]),
    ],
)
def test_form_rejects_invalid_content(
    prop: JsonObject, answer: str | list[str]
) -> None:
    form = AcpForm(
        schema.CreateFormSessionElicitationRequest.model_validate(
            {
                "sessionId": "native-1",
                "mode": "form",
                "message": "Question",
                "requestedSchema": {"type": "object", "properties": {"field": prop}},
            }
        )
    )
    assert form.request.requested_schema.properties is not None
    with pytest.raises(ValueError, match=r"validation error|form's choices"):
        form.value(form.request.requested_schema.properties["field"], answer)
