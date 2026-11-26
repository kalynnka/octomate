"""Inbound signals cross JSON without sharing runtime state."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import ValidationError
from uuid_utils.compat import uuid7

from octomate.schemas.awakes import UserMessageAwake
from octomate.schemas.conversation import ChannelAddress
from octomate.signals import (
    DeferredResponseSignal,
    InterruptRunSignal,
    Priming,
    RunAgentSignal,
    signal_adapter,
)

ADDRESS = ChannelAddress("web", "dm", "user", "user")


@pytest.fixture
def run_signal(priming: Priming) -> RunAgentSignal:
    return RunAgentSignal(
        priming=priming,
        conversation_id=uuid7(),
        user_id=None,
        conversation_address=ADDRESS,
        source_thread_address=ADDRESS,
        source_thread_message_ids=[uuid7()],
        run_name="react",
        user_prompt="hello",
    )


@pytest.fixture
def priming() -> Priming:
    return Priming(
        invocation_id=uuid7(),
        session_id="native-session",
        resume=True,
        model="selected-model",
        cwd=Path("/habitat/workspace"),
    )


@pytest.mark.parametrize("resume", [False, True])
def test_prepared_run_survives_json(
    run_signal: RunAgentSignal, priming: Priming, resume: bool
) -> None:
    payload = run_signal.model_dump()
    if resume:
        payload.update(user_prompt=None, deferred_batch_id=uuid7())
    signal = RunAgentSignal.model_validate(payload)
    received = signal_adapter.validate_json(signal_adapter.dump_json(signal))
    assert received == signal
    assert isinstance(received, RunAgentSignal)
    assert received.conversation_id == signal.conversation_id
    assert received.priming == priming


@pytest.mark.parametrize("missing", [True, False])
def test_run_requires_priming(run_signal: RunAgentSignal, missing: bool) -> None:
    payload = run_signal.model_dump()
    if missing:
        del payload["priming"]
    else:
        payload["priming"] = None
    with pytest.raises(ValidationError, match="priming"):
        signal_adapter.validate_python(payload)


def test_awake_is_not_an_agent_signal() -> None:
    awake = UserMessageAwake(messages=[])
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        signal_adapter.validate_python(asdict(awake))


@pytest.mark.parametrize("both", [False, True])
def test_run_requires_one_input(run_signal: RunAgentSignal, both: bool) -> None:
    payload = run_signal.model_dump()
    payload.update(
        user_prompt="hello" if both else None,
        deferred_batch_id=uuid7() if both else None,
    )
    with pytest.raises(ValidationError, match="exactly one"):
        RunAgentSignal.model_validate(payload)


def test_teleport_continuation_needs_no_human_batch(run_signal: RunAgentSignal) -> None:
    payload = run_signal.model_dump()
    payload.update(
        user_prompt=None, tool_results={"teleport-call": "Arrived in the workspace."}
    )
    signal = RunAgentSignal.model_validate(payload)
    received = signal_adapter.validate_json(signal_adapter.dump_json(signal))
    assert received == signal
    assert signal.deferred_batch_id is None
    payload["deferred_batch_id"] = uuid7()
    with pytest.raises(ValidationError, match="exactly one"):
        RunAgentSignal.model_validate(payload)


def test_continuation_rejects_empty_or_live_results(run_signal: RunAgentSignal) -> None:
    payload = run_signal.model_dump()
    payload.update(user_prompt=None, tool_results={})
    with pytest.raises(ValidationError, match="at least 1 item"):
        RunAgentSignal.model_validate(payload)
    payload["tool_results"] = {"teleport-call": RuntimeError("not a result")}
    with pytest.raises(ValidationError, match="valid string"):
        RunAgentSignal.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["invocation", "capabilities", "deferred_suspender", "model"]
)
def test_run_rejects_runtime_dependencies(
    run_signal: RunAgentSignal, field: str
) -> None:
    payload = run_signal.model_dump()
    payload[field] = lambda: None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunAgentSignal.model_validate(payload)


def test_controls_correlate_to_a_live_invocation_without_opening_a_turn(
    priming: Priming,
) -> None:
    invocation_id = uuid7()
    signals = [
        InterruptRunSignal(invocation_id=invocation_id),
        DeferredResponseSignal(invocation_id=invocation_id, batch_id=uuid7()),
    ]
    for signal in signals:
        received = signal_adapter.validate_json(signal_adapter.dump_json(signal))
        assert received == signal
        payload = signal.model_dump()
        payload["priming"] = priming.model_dump()
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            signal_adapter.validate_python(payload)
