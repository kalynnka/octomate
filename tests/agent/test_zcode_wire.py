from __future__ import annotations

import pytest
from pydantic import ValidationError

from octomate.tentacles.zcode.wire import (
    ContentDelta,
    ModelStreamingEvent,
    SessionMessages,
    TurnStartedEvent,
    run_event_adapter,
)
from octomate.types.json import JsonObject
from tests.support.zcode import event, history_message


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("model.streaming", {"kind": "text_delta"}),
        ("model.streaming", {"kind": "tool_input_start", "toolCallId": "tool"}),
        ("tool.updated", {"kind": "scheduled", "toolCallId": "tool"}),
        ("tool.updated", {"kind": "result", "toolCallId": "tool"}),
        ("turn.failed", {"error": {}}),
    ],
)
def test_supported_events_require_their_variant_fields(
    kind: str, payload: JsonObject
) -> None:
    with pytest.raises(ValidationError):
        event(kind, payload)


def test_turn_and_payload_are_discriminated_at_the_boundary() -> None:
    started = event(
        "turn.started", {"inputId": "input", "executionKind": "controlOnly"}
    )
    assert isinstance(started, TurnStartedEvent)
    assert started.payload.message_id is None
    delta = run_event_adapter.validate_json(
        event(
            "model.streaming",
            {"kind": "text_delta", "delta": "hello"},
        ).model_dump_json(by_alias=True)
    )
    assert isinstance(delta, ModelStreamingEvent)
    assert isinstance(delta.payload, ContentDelta)
    assert delta.payload.delta == "hello"


def test_history_skips_unconsumed_parts_but_rejects_malformed_supported_parts() -> None:
    message = history_message("answer", "visible")
    message["parts"] = [{"type": "text", "text": "visible"}, {"type": "step-start"}]
    history = SessionMessages.model_validate({"messages": [message]})
    assert len(history.messages[0].parts) == 1
    message["parts"] = [{"type": "text"}]
    with pytest.raises(ValidationError):
        SessionMessages.model_validate({"messages": [message]})


@pytest.mark.parametrize(
    ("user", "kind", "expected"),
    [
        (False, "assistant_response", True),
        (False, "system_reminder", False),
        (True, "assistant_response", False),
        (True, "system_reminder", False),
    ],
)
def test_runtime_origin_visibility_depends_on_role_and_kind(
    user: bool, kind: str, expected: bool
) -> None:
    message = history_message("message", "content", user=user)
    info = message["info"]
    assert isinstance(info, dict)
    info["semantics"] = {
        "origin": "agent_runtime",
        "kind": kind,
        "uiVisibility": "visible",
        "transcriptVisibility": "visible",
        "providerVisibility": "visible",
    }
    parsed = SessionMessages.model_validate({"messages": [message]})
    assert parsed.messages[0].info.visible is expected


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("synthetic", False),
        ("model-only", False),
        ("hidden-ui", False),
        ("debug", False),
        ("hidden-transcript", False),
        ("legacy", True),
    ],
)
def test_assistant_visibility_respects_flags_and_legacy_history(
    flag: str, expected: bool
) -> None:
    message = history_message("answer", "content")
    info = message["info"]
    assert isinstance(info, dict)
    semantics = info["semantics"]
    assert isinstance(semantics, dict)
    if flag == "synthetic":
        info["synthetic"] = True
    elif flag == "model-only":
        info["visibility"] = "model-only"
    elif flag == "legacy":
        del info["semantics"]
    elif flag == "hidden-transcript":
        semantics["transcriptVisibility"] = "hidden"
    else:
        semantics["uiVisibility"] = "debug" if flag == "debug" else "hidden"
    parsed = SessionMessages.model_validate({"messages": [message]})
    assert parsed.messages[0].info.visible is expected
