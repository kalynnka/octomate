from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from logfire.testing import CaptureLogfire
from logfire.testing import capfire as capfire
from openai_codex.client import CodexClient
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span
from pydantic_ai.messages import TextContent
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.telemetry import (
    agent_input_message_attributes,
    octomate_logfire,
    octomate_trace_environment,
)
from octomate.tentacles.codex.telemetry import TracedCodexClient
from octomate.tentacles.inkling import InklingTentacle
from octomate.types.json import JsonObject
from tests.support.managers import FakeConversationManager


def test_agent_input_messages_render_every_user_prompt_segment() -> None:
    attributes = agent_input_message_attributes(
        [
            TextContent(content="@Octomate"),
            "summon claude+fable in a new thread.",
        ]
    )

    assert json.loads(attributes["gen_ai.input.messages"]) == [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "@Octomate"},
                {
                    "type": "text",
                    "content": "summon claude+fable in a new thread.",
                },
            ],
        }
    ]
    assert json.loads(attributes["logfire.json_schema"]) == {
        "type": "object",
        "properties": {"gen_ai.input.messages": {"type": "array"}},
    }


async def test_reused_codex_client_propagates_each_concurrent_kick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[JsonObject] = []

    def capture(client: CodexClient, payload: JsonObject) -> None:
        captured.append(payload)

    monkeypatch.setattr(CodexClient, "_write_message", capture)
    client = TracedCodexClient()

    async def kick(trace_id: int) -> None:
        context = SpanContext(
            trace_id=trace_id,
            span_id=trace_id + 1,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        with use_span(NonRecordingSpan(context)):
            await asyncio.sleep(0)
            await asyncio.to_thread(
                client._write_message, {"id": trace_id, "method": "turn/start"}
            )

    await asyncio.gather(kick(11), kick(22))
    for payload in captured:
        trace_id = payload["id"]
        assert isinstance(trace_id, int)
        assert payload["trace"] == {
            "traceparent": f"00-{trace_id:032x}-{trace_id + 1:016x}-01"
        }
    client._write_message({"id": 33, "method": "turn/start"})
    assert "trace" not in captured[-1]


async def test_inkling_native_model_spans_keep_the_driving_trace(
    capfire: CaptureLogfire,
) -> None:
    agent = Agent(
        TestModel(custom_output_text="done"),
        deps_type=type(None),
        output_type=[str, list[MessageSegment], DeferredToolRequests],
    )
    agent.instrument = True
    tentacle = InklingTentacle(
        "inkling", Octomate(conversations=FakeConversationManager()), agent=agent
    )

    with use_span(NonRecordingSpan(SpanContext(101, 102, False, TraceFlags(1)))):
        result = await tentacle.run(
            "hello",
            conversation_address=ChannelAddress(
                channel_tentacle_id="test",
                chat_type="dm",
                chat_id="test",
                user_id="test",
            ),
            thread_id=uuid.uuid4(),
            output_type=str,
        )

    assert result.output == "done"
    model_spans = [
        span
        for span in capfire.exporter.exported_spans
        if span.attributes and span.attributes.get("gen_ai.operation.name") == "chat"
    ]
    assert model_spans
    for span in model_spans:
        assert span.context is not None
        assert span.context.trace_id == 101
        assert span.parent is not None


def test_native_exporter_uses_the_configured_logfire_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = octomate_logfire.config
    monkeypatch.setattr(config, "send_to_logfire", True)
    monkeypatch.setattr(config, "token", "test-token")
    monkeypatch.setattr(config.advanced, "base_url", "https://logfire.example")
    trace_environment = octomate_trace_environment()
    assert trace_environment is not None
    assert trace_environment.endpoint == "https://logfire.example/v1/traces"
    assert "test-token" not in repr(trace_environment)
    env = trace_environment.as_env()
    assert (
        env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "https://logfire.example/v1/traces"
    )
    assert env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == "Authorization=test-token"
    assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    monkeypatch.setattr(config, "send_to_logfire", False)
    assert octomate_trace_environment() is None
