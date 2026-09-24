"""Harness naming through real Inkling turns and disposable Arcanus persistence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai_harness.step_persistence.conversations import conversation_text
from pydantic_ai_harness.step_persistence.naming import NamingResult

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling import base as inkling_base
from tests.support.managers import a_thread

pytestmark = pytest.mark.usefixtures("in_memory_engine")

ADDRESS = ChannelAddress(
    channel_tentacle_id="test", chat_type="dm", chat_id="chat", user_id="test"
)


@pytest.fixture(autouse=True)
def inkling_session_names() -> None:
    """Override the suite's isolation fixture: these tests run Harness naming."""


@pytest.fixture
def naming_requests() -> list[str]:
    return []


@pytest.fixture
def naming_model(naming_requests: list[str]) -> FunctionModel:
    def name(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.function_tools
        assert info.model_settings is not None
        assert info.model_settings.get("max_tokens") == 250
        naming_requests.append(conversation_text(messages))
        title = (
            "Plan gateway MCP exposure"
            if len(naming_requests) == 1
            else "Fix session naming updates"
        )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"title": title})]
        )

    async def reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        if info.output_tools:
            yield {
                0: DeltaToolCall(
                    name=info.output_tools[0].name,
                    json_args=json.dumps(
                        {
                            "response": [
                                {
                                    "type": "markdown",
                                    "data": {"text": "Expose MCP tools."},
                                }
                            ]
                        }
                    ),
                )
            }
            return
        yield "Expose MCP tools."

    return FunctionModel(function=name, stream_function=reply)


async def test_new_prompts_name_and_revise_the_session_and_thread(
    naming_model: FunctionModel, naming_requests: list[str]
) -> None:
    octomate = Octomate()
    tentacle = InklingTentacle("inkling", octomate, models={"test": naming_model})
    thread_id = await a_thread()

    first = await tentacle.run(
        "Investigate the gateway",
        conversation_address=ADDRESS,
        thread_id=thread_id,
        output_type=str,
    )
    conversation = await octomate.conversations.ensure(
        thread_id, agent_tentacle_id="inkling"
    )
    assert conversation.name == "Plan gateway MCP exposure"
    thread = await octomate.thread_manager.get(thread_id)
    assert thread is not None
    assert thread.title == conversation.name
    assert "user: Investigate the gateway" in naming_requests[0]
    assert "assistant: Expose MCP tools." not in naming_requests[0]
    assert len(conversation.runs) == 1
    assert conversation_text(conversation.messages) == conversation_text(
        first.all_messages()
    )

    await tentacle.run(
        "Old context " * 300 + "Now fix session naming",
        conversation_address=ADDRESS,
        thread_id=thread_id,
        output_type=str,
    )
    revised = await octomate.conversations.get(conversation.id)
    thread = await octomate.thread_manager.get(thread_id)
    assert revised.name == "Fix session naming updates"
    assert thread is not None
    assert thread.title == revised.name
    assert len(naming_requests) == 2
    assert "Previous title: Plan gateway MCP exposure" in naming_requests[1]
    assert "Now fix session naming" in naming_requests[1]
    assert len(naming_requests[1].split("Current conversation tail:\n", 1)[1]) == 2400
    assert len(revised.runs) == 2


@pytest.mark.parametrize("configured", [True, False])
async def test_naming_model_override_and_active_model_default(
    naming_model: FunctionModel, naming_requests: list[str], configured: bool
) -> None:
    octomate = Octomate()
    tentacle = InklingTentacle(
        "inkling",
        octomate,
        models={"default": TestModel()},
        naming_model=naming_model if configured else None,
    )
    await tentacle.run(
        "Investigate this",
        conversation_address=ADDRESS,
        thread_id=await a_thread(),
        model=TestModel() if configured else naming_model,
        output_type=str,
    )
    assert len(naming_requests) == 1
    assert "user: Investigate this" in naming_requests[0]


async def test_naming_and_foreground_stream_run_concurrently(
    naming_model: FunctionModel, naming_requests: list[str]
) -> None:
    naming_started = asyncio.Event()
    agent_started = asyncio.Event()
    reply_delivered = asyncio.Event()
    finish_naming = asyncio.Event()

    async def name(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        naming_requests.append(conversation_text(messages))
        naming_started.set()
        await agent_started.wait()
        await finish_naming.wait()
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"title": "Gateway plan"})]
        )

    async def reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        agent_started.set()
        await naming_started.wait()
        yield "The new reply."

    octomate = Octomate()
    tentacle = InklingTentacle("inkling", octomate, models={"default": naming_model})
    thread_id = await a_thread()
    await tentacle.run(
        "Prior question",
        conversation_address=ADDRESS,
        thread_id=thread_id,
        output_type=str,
    )
    naming_requests.clear()
    tentacle.naming_model = FunctionModel(function=name)

    async def consume() -> None:
        async with tentacle.run_stream_events(
            "New question",
            conversation_address=ADDRESS,
            thread_id=thread_id,
            model=FunctionModel(stream_function=reply),
            output_type=str,
        ) as stream:
            async for event in stream:
                if isinstance(event, AgentRunResultEvent):
                    reply_delivered.set()

    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
        task = tasks.create_task(consume())
        await reply_delivered.wait()
        assert not task.done()
        assert "user: Prior question" in naming_requests[0]
        assert "assistant: Expose MCP tools." in naming_requests[0]
        assert "user: New question" in naming_requests[0]
        assert "The new reply." not in naming_requests[0]
        finish_naming.set()

    thread = await octomate.thread_manager.get(thread_id)
    assert thread is not None
    assert thread.title == "Gateway plan"


async def test_child_name_does_not_replace_the_parent_thread_title(
    naming_model: FunctionModel,
) -> None:
    octomate = Octomate()
    tentacle = InklingTentacle("inkling", octomate, models={"test": naming_model})
    thread_id = await a_thread()
    parent = await octomate.conversations.ensure(thread_id, agent_tentacle_id="inkling")
    child = await octomate.conversations.ensure(
        thread_id,
        agent_tentacle_id="inkling",
        subagent_id="research",
        parent_conversation_id=parent.id,
    )
    thread = await octomate.thread_manager.get(thread_id)
    assert thread is not None
    await octomate.thread_manager.rename(thread, "Parent work")
    await tentacle.subagent_run(
        "Investigate the gateway",
        conversation_address=ADDRESS,
        thread_id=thread_id,
        conversation_id=child.id,
    )
    named = await octomate.conversations.get(child.id)
    assert named.name == "Plan gateway MCP exposure"
    thread = await octomate.thread_manager.get(thread_id)
    assert thread is not None
    assert thread.title == "Parent work"


@pytest.mark.parametrize("error", [RuntimeError("provider failed"), TimeoutError()])
async def test_naming_failure_preserves_the_reply_and_existing_name(
    naming_model: FunctionModel, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    octomate = Octomate()
    tentacle = InklingTentacle("inkling", octomate, models={"test": naming_model})
    thread_id = await a_thread()
    conversation = await octomate.conversations.ensure(
        thread_id, agent_tentacle_id="inkling"
    )
    await octomate.conversations.set_name(conversation, "Existing name")
    monkeypatch.setattr(inkling_base, "generate_name", AsyncMock(side_effect=error))
    result = await tentacle.run(
        "Continue", conversation_address=ADDRESS, thread_id=thread_id, output_type=str
    )
    assert result.output == "Expose MCP tools."
    saved = await octomate.conversations.get(conversation.id)
    assert saved.name == "Existing name"
    assert len(saved.runs) == 1


async def test_cancelling_a_run_cleans_up_its_naming_task(
    naming_model: FunctionModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def name(*, model: Model | str, prompt: str) -> NamingResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("naming should be cancelled")

    tentacle = InklingTentacle("inkling", Octomate(), models={"test": naming_model})
    monkeypatch.setattr(inkling_base, "generate_name", name)
    task = asyncio.create_task(
        tentacle.run(
            "Continue",
            conversation_address=ADDRESS,
            thread_id=await a_thread(),
            output_type=str,
        )
    )
    async with asyncio.timeout(3):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()


async def test_a_prompt_names_a_deferred_turn_but_its_resume_does_not(
    naming_model: FunctionModel, naming_requests: list[str]
) -> None:
    tentacle = InklingTentacle(
        "inkling",
        Octomate(),
        models={"test": TestModel(call_tools=["ask_questions"])},
        naming_model=naming_model,
        capabilities=[AskCapability()],
    )
    thread_id = await a_thread()
    result = await tentacle.run(
        "Ask before proceeding",
        conversation_address=ADDRESS,
        thread_id=thread_id,
    )
    assert isinstance(result.output, DeferredToolRequests)
    assert len(naming_requests) == 1
    await tentacle.run(
        conversation_address=ADDRESS,
        thread_id=thread_id,
        deferred_tool_results=DeferredToolResults(
            calls={call.tool_call_id: "Proceed" for call in result.output.calls}
        ),
        model=TestModel(call_tools=[], custom_output_text="Done"),
        output_type=str,
    )
    assert len(naming_requests) == 1
