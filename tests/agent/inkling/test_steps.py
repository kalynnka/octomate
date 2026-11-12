"""Configured Inkling turns share their archive identity with upstream checkpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent, RunContext
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.toolsets import FunctionToolset

from octomate import Octomate
from octomate.config import ModelConfig, ProvidersConfig
from octomate.config.agents import InklingConfig
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager, ConversationStore
from octomate.managers.steps import StepStore
from octomate.providers import ProviderRegistry
from octomate.schemas.conversation import ChannelAddress, Conversation, ConversationRun
from octomate.schemas.runs import AgentRun
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.inkling.build import build_inkling
from octomate.tentacles.inkling.react import (
    ReactDeps,
    ReactState,
    StartTurn,
    build_react_graph,
    iter_react_graph_events,
)

pytestmark = pytest.mark.usefixtures("in_memory_engine")

ADDRESS = ChannelAddress("test", "dm", "steps", "user")


@pytest.fixture
def inkling() -> InklingTentacle:
    host = Octomate()
    return build_inkling(
        "inkling",
        InklingConfig(models=[ModelConfig(name="test")], gateway=False),
        host,
        registry=ProviderRegistry(ProvidersConfig()),
        auth=host.auth,
        habitats=host.habitats,
    )


@pytest.fixture
async def conversation(inkling: InklingTentacle) -> Conversation:
    thread = await inkling.threads.ensure(ADDRESS)
    return await inkling.conversations.ensure(thread.id, agent_tentacle_id=inkling.id)


async def assert_completed_run(
    conversation: Conversation, result: AgentRunResult[InklingOutput]
) -> None:
    store = StepStore()
    restored = await ConversationStore(ConversationManager()).load(str(conversation.id))
    assert restored is not None
    assert restored.revision is not None
    assert restored.revision == result.conversation.revision
    assert restored.usage == result.usage
    assert restored.new_message_index == len(result.all_messages())
    assert restored.deferred_tool_requests == (
        result.output if isinstance(result.output, DeferredToolRequests) else None
    )
    record = await store.get_run(run_id=result.run_id)
    assert record is not None
    assert record.conversation_id == str(conversation.id)
    assert record.agent_name == "octomate-inkling"
    assert record.registration_id is not None
    snapshot = await store.latest_snapshot(run_id=result.run_id)
    if isinstance(result.output, DeferredToolRequests):
        assert snapshot is None
    else:
        assert snapshot is not None
        assert snapshot.conversation_id == str(conversation.id)
        assert [message.parts for message in snapshot.messages] == [
            message.parts for message in result.all_messages()
        ]
    events = await store.list_events(run_id=result.run_id)
    assert events[0].kind == "run_started"
    assert events[-1].kind == "run_completed"
    assert all(entry.conversation_id == str(conversation.id) for entry in events)
    async with async_session() as session:
        run = await session.get(AgentRun, result.run_id)
        assert run is not None
        assert run.started_at == record.started_at
        assert run.name == "react"
        assert [message.parts for message in run.messages] == [
            message.parts for message in result.new_messages()
        ]
        assert all(message.run_id == result.run_id for message in run.messages)
        links = await session.list(
            ConversationRun, expressions=[ConversationRun["run_id"] == result.run_id]
        )
        assert len(links) == 1
        assert links[0].conversation_id == conversation.id


@pytest.mark.parametrize("streaming", [False, True])
async def test_supplied_conversation_must_belong_to_the_agent(
    inkling: InklingTentacle, conversation: Conversation, streaming: bool
) -> None:
    foreign = await inkling.conversations.ensure(
        conversation.thread_id, agent_tentacle_id="other"
    )
    if streaming:
        async with inkling.run_stream_events(
            "continue",
            conversation=foreign,
            conversation_address=ADDRESS,
            model=TestModel(),
        ) as stream:
            with pytest.raises(ValueError, match="does not belong"):
                await anext(stream)
    else:
        with pytest.raises(ValueError, match="does not belong"):
            await inkling.run(
                "continue",
                conversation=foreign,
                conversation_address=ADDRESS,
                model=TestModel(),
            )
    assert await StepStore().list_runs() == []


@pytest.mark.parametrize("streaming", [False, True])
async def test_configured_turns_register_before_tools_and_archive_once(
    inkling: InklingTentacle,
    conversation: Conversation,
    streaming: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    load = AsyncMock(wraps=inkling.conversations.get)
    monkeypatch.setattr(inkling.conversations, "get", load)
    called: list[str] = []

    async def lookup(ctx: RunContext[None]) -> str:
        assert ctx.run_id is not None
        assert await StepStore().get_run(run_id=ctx.run_id) is not None
        called.append(ctx.run_id)
        return "found"

    result: AgentRunResult[InklingOutput] | None = None
    if streaming:
        async with inkling.run_stream_events(
            "look up",
            conversation_address=ADDRESS,
            conversation=context,
            output_type=str,
            model=TestModel(call_tools=["lookup"]),
            toolsets=[FunctionToolset([lookup])],
        ) as stream:
            async for event in stream:
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
    else:
        result = await inkling.run(
            "look up",
            conversation_address=ADDRESS,
            conversation=context,
            output_type=str,
            model=TestModel(call_tools=["lookup"]),
            toolsets=[FunctionToolset([lookup])],
        )
    load.assert_not_awaited()
    assert result is not None
    await assert_completed_run(conversation, result)
    assert called == [result.run_id]
    store = StepStore()
    events = await store.list_events(run_id=result.run_id)
    tool_event = next(entry for entry in events if entry.kind == "tool_call_completed")
    assert tool_event.tool_call_id is not None
    effect = await store.get_tool_effect(
        run_id=result.run_id, tool_call_id=tool_event.tool_call_id
    )
    assert effect is not None
    assert effect.status == "completed"
    assert await store.list_unresolved_tool_effects(run_id=result.run_id) == []

    second = await inkling.run(
        "continue",
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        output_type=str,
        model=TestModel(call_tools=[]),
    )
    await assert_completed_run(conversation, second)
    assert second.usage.requests == result.usage.requests + 1
    assert second.usage.tool_calls == result.usage.tool_calls == 1
    assert second.conversation.revision != result.conversation.revision
    assert [record.run_id for record in await store.list_runs()] == [
        result.run_id,
        second.run_id,
    ]


@pytest.mark.parametrize("failing", [False, True])
async def test_nonstreaming_graph_uses_the_same_store(
    inkling: InklingTentacle, conversation: Conversation, failing: bool
) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if failing:
            raise RuntimeError("model failed")
        return ModelResponse(parts=[TextPart("done")])

    start = StartTurn[InklingOutput, None](user_prompt="hello")
    operation = build_react_graph(start).run(
        inputs=start,
        state=ReactState(
            conversation_address=ADDRESS,
            conversation=await inkling.resolve_conversation(
                conversation.thread_id,
                conversation_address=ADDRESS,
                conversation_id=conversation.id,
            ),
        ),
        deps=ReactDeps(
            agent=inkling.agent,
            conversation_manager=inkling.conversations,
            agent_deps=None,
            capabilities=inkling.capabilities,
            model=FunctionModel(respond),
            output_type=str,
        ),
    )
    if failing:
        with pytest.raises(RuntimeError, match="model failed"):
            await operation
        store = StepStore()
        [record] = await store.list_runs()
        events = await store.list_events(run_id=record.run_id)
        assert [entry.kind for entry in events] == [
            "run_started",
            "model_request_started",
            "model_request_failed",
            "run_failed",
        ]
        async with async_session() as session:
            run = await session.get(AgentRun, record.run_id)
            assert run is not None
            assert run.name == "react"
            assert len(run.messages) == 1
        return
    result = await operation
    await assert_completed_run(conversation, result)


@pytest.mark.parametrize("after_tool", [False, True])
async def test_model_failure_preserves_the_registered_run_and_settled_checkpoint(
    inkling: InklingTentacle, conversation: Conversation, after_tool: bool
) -> None:
    requests = 0

    async def fail(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        nonlocal requests
        requests += 1
        if after_tool and requests == 1:
            yield {0: DeltaToolCall(name="lookup", json_args="{}")}
            return
        raise RuntimeError("model failed")

    def lookup() -> str:
        return "settled result"

    with pytest.raises(RuntimeError, match="model failed"):
        await inkling.run(
            "go",
            conversation_address=ADDRESS,
            conversation=await inkling.resolve_conversation(
                conversation.thread_id, conversation_address=ADDRESS
            ),
            output_type=str,
            model=FunctionModel(stream_function=fail),
            toolsets=[FunctionToolset([lookup])],
        )
    store = StepStore()
    [record] = await store.list_runs()
    events = await store.list_events(run_id=record.run_id)
    assert events[-1].kind == "run_failed"
    assert [entry.kind for entry in events].count("model_request_started") == requests
    assert [entry.kind for entry in events].count("model_request_completed") == (
        requests - 1
    )
    snapshot = await store.latest_snapshot(run_id=record.run_id)
    assert (snapshot is not None) == after_tool
    assert await store.list_unresolved_tool_effects(run_id=record.run_id) == []
    async with async_session() as session:
        assert await session.count(AgentRun) == 1
        assert await session.count(ConversationRun) == 1
        run = await session.get(AgentRun, record.run_id)
        assert run is not None
        assert run.messages
        assert run.name == "react"
        if snapshot is not None:
            assert snapshot.state == "complete"
            assert [message.parts for message in snapshot.messages] == [
                message.parts for message in run.messages
            ]


async def test_deferred_resume_registers_a_new_run_in_the_same_conversation(
    inkling: InklingTentacle, conversation: Conversation
) -> None:
    first = await inkling.run(
        "ask before proceeding",
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        model=TestModel(call_tools=["ask_questions"]),
    )
    assert isinstance(first.output, DeferredToolRequests)
    await assert_completed_run(conversation, first)
    second = await inkling.run(
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        output_type=str,
        deferred_tool_results=DeferredToolResults(
            calls={call.tool_call_id: "proceed" for call in first.output.calls}
        ),
        model=TestModel(call_tools=[]),
    )
    await assert_completed_run(conversation, second)
    assert second.usage.requests == first.usage.requests + 1
    assert second.run_id != first.run_id
    store = StepStore()
    assert [record.run_id for record in await store.list_runs()] == [
        first.run_id,
        second.run_id,
    ]
    events = await store.list_events(run_id=first.run_id)
    assert events[-1].kind == "run_completed"
    async with async_session() as session:
        assert await session.count(AgentRun) == 2
        assert await session.count(ConversationRun) == 2
        deferred = await session.get(AgentRun, first.run_id)
        assert deferred is not None
        assert [message.parts for message in deferred.messages] == [
            message.parts for message in first.new_messages()
        ]


async def test_tool_failure_is_recorded_when_the_model_can_continue(
    inkling: InklingTentacle, conversation: Conversation
) -> None:
    def broken() -> str:
        raise RuntimeError("tool failed")

    result = await inkling.run(
        "use the tool",
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        output_type=str,
        model=TestModel(call_tools=["broken"]),
        toolsets=[FunctionToolset([broken])],
    )
    await assert_completed_run(conversation, result)
    store = StepStore()
    events = await store.list_events(run_id=result.run_id)
    failed = next(entry for entry in events if entry.kind == "tool_call_failed")
    assert failed.tool_call_id is not None
    effect = await store.get_tool_effect(
        run_id=result.run_id, tool_call_id=failed.tool_call_id
    )
    assert effect is not None
    assert effect.status == "failed"
    assert await store.list_unresolved_tool_effects(run_id=result.run_id) == []


@pytest.mark.parametrize("streaming", [False, True])
async def test_failed_continuation_saves_usage_without_rearchiving_prior_turns(
    inkling: InklingTentacle, conversation: Conversation, streaming: bool
) -> None:
    first = await inkling.run(
        "remember",
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        output_type=str,
        model=TestModel(call_tools=[]),
    )
    prior = await inkling.conversations.history(conversation.id)
    requests = 0

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal requests
        requests += 1
        if requests == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {}, "lookup-1")])
        raise RuntimeError("continuation failed")

    async def stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[dict[int, DeltaToolCall]]:
        reply(messages, info)
        yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="lookup-1")}

    def lookup() -> str:
        return "retained tool result"

    start = StartTurn[InklingOutput, None](user_prompt="continue")
    state = ReactState(
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id,
            conversation_address=ADDRESS,
            conversation_id=conversation.id,
        ),
    )
    deps = ReactDeps(
        agent=inkling.agent,
        conversation_manager=inkling.conversations,
        agent_deps=None,
        capabilities=inkling.capabilities,
        model=FunctionModel(function=reply, stream_function=stream),
        toolsets=[FunctionToolset([lookup])],
        output_type=str,
    )
    if streaming:
        with pytest.raises(RuntimeError, match="continuation failed"):
            [_ async for _ in iter_react_graph_events(start, state=state, deps=deps)]
    else:
        with pytest.raises(RuntimeError, match="continuation failed"):
            await build_react_graph(start).run(inputs=start, state=state, deps=deps)

    restored = await inkling.conversations.get(
        conversation.id, with_runs=False, with_messages=True
    )
    assert restored.revision != first.conversation.revision
    assert restored.usage.requests >= first.usage.requests + 1
    assert restored.usage.tool_calls == first.usage.tool_calls + 1
    assert [message.id for message in restored.messages[: len(prior)]] == [
        message.id for message in prior
    ]
    assert len(restored.messages) == len(prior) + 3
    assert "retained tool result" in str(restored.messages[-1])
    records = await StepStore().list_runs()
    assert len(records) == 2
    events = await StepStore().list_events(run_id=records[-1].run_id)
    assert events[-1].kind == "run_failed"


async def test_graph_rejects_a_result_from_a_stale_conversation(
    inkling: InklingTentacle, conversation: Conversation
) -> None:
    async def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        current = await inkling.conversations.get(
            conversation.id, with_runs=False, with_messages=True
        )
        await ConversationStore(inkling.conversations).record_conversation(current)
        return ModelResponse(parts=[TextPart("stale reply")])

    start = StartTurn[InklingOutput, None](user_prompt="stale prompt")
    with pytest.raises(ConversationConflict):
        await build_react_graph(start).run(
            inputs=start,
            state=ReactState(
                conversation_address=ADDRESS,
                conversation=await inkling.resolve_conversation(
                    conversation.thread_id,
                    conversation_address=ADDRESS,
                    conversation_id=conversation.id,
                ),
            ),
            deps=ReactDeps(
                agent=inkling.agent,
                conversation_manager=inkling.conversations,
                agent_deps=None,
                capabilities=inkling.capabilities,
                model=FunctionModel(reply),
                output_type=str,
            ),
        )
    assert await inkling.conversations.history(conversation.id) == []


async def test_subagent_inherits_persistence_and_records_parent_lineage(
    inkling: InklingTentacle, conversation: Conversation
) -> None:
    child = await inkling.conversations.ensure(
        conversation.thread_id,
        agent_tentacle_id=inkling.id,
        subagent_id="helper",
        parent_conversation_id=conversation.id,
    )
    results: list[AgentRunResult[InklingOutput]] = []

    async def delegate() -> str:
        result = await inkling.subagent_run(
            "help",
            conversation_address=ADDRESS,
            conversation=await inkling.resolve_conversation(
                conversation.thread_id,
                conversation_address=ADDRESS,
                conversation_id=child.id,
            ),
            model=TestModel(call_tools=[]),
        )
        results.append(result)
        return "delegated"

    parent = await inkling.run(
        "delegate",
        conversation_address=ADDRESS,
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        output_type=str,
        model=TestModel(call_tools=["delegate"]),
        toolsets=[FunctionToolset([delegate])],
    )
    assert results, parent.output
    [result] = results
    await assert_completed_run(conversation, parent)
    await assert_completed_run(child, result)
    record = await StepStore().get_run(run_id=result.run_id)
    assert record is not None
    assert record.parent_run_id == parent.run_id
