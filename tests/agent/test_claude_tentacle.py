from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import Message
from pydantic import TypeAdapter
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartStartEvent,
)

from pydantic_ai.tools import DeferredToolRequests

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    DirectAnswerDecision,
    TriageDecision,
    TriageDecisionAdapter,
)
from octomate.tentacles.agent.claude import ClaudeCodeTentacle
from octomate.tentacles.agent.claude import base as claude_base
from octomate.tentacles.agent.claude.adapter import ClaudeRunAccumulator
from tests.support.managers import FakeConversation, FakeConversationManager

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="private", chat_id="alice", user_id="alice"
)


class FakeClaudeClient:
    """Stands in for `ClaudeSDKClient`: captures the options + prompt and yields
    a scripted Claude message stream (text, a tool round-trip, a final answer)."""

    last_options: object = None
    last_prompt: str | None = None

    def __init__(self, options: object = None, transport: object = None) -> None:
        FakeClaudeClient.last_options = options

    async def __aenter__(self) -> FakeClaudeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        FakeClaudeClient.last_prompt = prompt

    async def receive_response(self) -> AsyncIterator[Message]:
        yield AssistantMessage(
            content=[
                TextBlock(text="reading"),
                ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"}),
            ],
            model="claude-opus-4-8",
        )
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")])
        yield AssistantMessage(
            content=[TextBlock(text="done")], model="claude-opus-4-8"
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-xyz",
            result="done",
        )


def _tentacle(conversations: FakeConversationManager) -> ClaudeCodeTentacle:
    return ClaudeCodeTentacle(
        "claude", Octomate(conversations=conversations), config=ClaudeCodeConfig()
    )


async def test_run_stream_events_proxies_events_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    events = []
    async with tentacle.run_stream_events(
        "fix it", conversation_address=KEY, run_name="reception"
    ) as stream:
        async for event in stream:
            events.append(event)

    # Live events proxied for the channel feelers, then a terminal result.
    kinds = {type(e) for e in events}
    assert {PartStartEvent, FunctionToolCallEvent, FunctionToolResultEvent} <= kinds
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "done"
    assert FakeClaudeClient.last_prompt == "fix it"

    # One run persisted; the session id captured from ResultMessage is stored on
    # the conversation for resume.
    assert len(conversations.runs) == 1
    fake, _label, messages = conversations.runs[0]
    assert fake.external_id == "sess-xyz"
    assert messages  # user prompt + assistant turns


async def test_run_resumes_prior_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    conversations.store[(KEY, "claude")] = FakeConversation(external_id="prev-sess")
    tentacle = _tentacle(conversations)

    result = await tentacle.run("again", conversation_address=KEY)

    assert result.output == "done"
    options = FakeClaudeClient.last_options
    assert getattr(options, "resume", None) == "prev-sess"


class StructuredClaudeClient(FakeClaudeClient):
    """A run that returns a JSON structured result (an `output_format` run),
    e.g. Claude acting as the triage agent emitting a TriageDecision."""

    async def receive_response(self) -> AsyncIterator[Message]:
        yield AssistantMessage(content=[TextBlock(text="deciding")], model="m")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="",
            structured_output={
                "action": "direct_answer",
                "target_id": "im",
                "answer": "hi there",
                "reason": "simple answer",
            },
        )


class LiteralClaudeClient(FakeClaudeClient):
    async def receive_response(self) -> AsyncIterator[Message]:
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="literal-session",
            result="",
            structured_output="accepted",
        )


async def test_run_with_output_type_returns_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", StructuredClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    result = await tentacle.run(
        "triage this",
        conversation_address=KEY,
        output_type=TriageDecision,
    )

    assert isinstance(result.output, DirectAnswerDecision)
    assert result.output.action == "direct_answer"
    assert result.output.answer == "hi there"
    assert getattr(StructuredClaudeClient.last_options, "output_format", None) == {
        "type": "json_schema",
        "schema": TriageDecisionAdapter.json_schema(),
    }


async def test_run_uses_literal_output_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", LiteralClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    result = await tentacle.run(
        "triage this",
        conversation_address=KEY,
        output_type=Literal["accepted"],
    )

    assert result.output == "accepted"
    assert getattr(LiteralClaudeClient.last_options, "output_format", None) == {
        "type": "json_schema",
        "schema": TypeAdapter(Literal["accepted"]).json_schema(),
    }


async def test_run_extracts_structured_candidate_from_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", StructuredClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    result = await tentacle.run(
        "triage this",
        conversation_address=KEY,
        output_type=TriageDecision | Literal["ignored"],
    )

    assert isinstance(result.output, DirectAnswerDecision)
    assert getattr(StructuredClaudeClient.last_options, "output_format", None) == {
        "type": "json_schema",
        "schema": TypeAdapter(TriageDecision | Literal["ignored"]).json_schema(),
    }


async def test_run_rejects_deferred_output_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", StructuredClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    with pytest.raises(ValueError, match="DeferredToolRequests"):
        await tentacle.run(
            "triage this",
            conversation_address=KEY,
            output_type=[TriageDecision, DeferredToolRequests],
        )


async def test_run_honors_per_run_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    await tentacle.run("hi", conversation_address=KEY, model="claude-opus-4-5")

    assert getattr(FakeClaudeClient.last_options, "model", None) == "claude-opus-4-5"


def test_build_structured_result_validates_into_model() -> None:
    accumulator = ClaudeRunAccumulator()
    accumulator.structured_output = {
        "action": "direct_answer",
        "target_id": "im",
        "answer": "done",
        "reason": "simple answer",
    }

    result = accumulator.build_structured_result(
        TriageDecisionAdapter, run_id="r1", conversation_id="c1"
    )

    assert isinstance(result.output, DirectAnswerDecision)
    assert result.output.action == "direct_answer"
