"""Configured Inkling turns share their archive identity with upstream checkpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import nullcontext
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent, RunContext
from pydantic_ai.conversation_store import ConversationConflict
from pydantic_ai.messages import (
    CompactionPart,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
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
from octomate.schemas.compactions import Compaction, CompactionMembership
from octomate.schemas.conversation import ChannelAddress, Conversation, ConversationRun
from octomate.schemas.messages import ModelResponse as StoredModelResponse
from octomate.schemas.runs import AgentRun
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.inkling.build import build_inkling
from octomate.tentacles.inkling.capabilities.compaction import SummarizingCompaction
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
        interrupted = await store.latest_snapshot(
            run_id=result.run_id, include_interrupted=True
        )
        assert interrupted is not None
        assert interrupted.state == "interrupted"
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
    assert snapshot is not None
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


@pytest.fixture
def summarizing(inkling: InklingTentacle) -> SummarizingCompaction[None]:
    capability = next(
        cap for cap in inkling.capabilities if isinstance(cap, SummarizingCompaction)
    )
    strategy = SummarizingCompaction[None](
        model=TestModel(custom_output_text="The earlier work is summarized."),
        max_messages=2,
        keep_messages=1,
        preserve_first_user_message=False,
    )
    inkling.capabilities[inkling.capabilities.index(capability)] = strategy
    return strategy


@pytest.mark.parametrize("streaming", [False, True])
async def test_compaction_archives_originals_and_continues_from_summary(
    inkling: InklingTentacle,
    conversation: Conversation,
    summarizing: SummarizingCompaction[None],
    streaming: bool,
) -> None:
    first = await inkling.run(
        "old question " * 200,
        conversation=conversation,
        conversation_address=ADDRESS,
        output_type=str,
        model=TestModel(call_tools=[], custom_output_text="old answer " * 200),
    )
    loaded = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    original_ids = [message.id for message in loaded.messages]
    received: list[ModelMessage] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        received.extend(messages)
        return ModelResponse(parts=[TextPart("continued")])

    async def stream_reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        received.extend(messages)
        yield "continued"

    model = FunctionModel(reply, stream_function=stream_reply)
    result: AgentRunResult[InklingOutput] | None = None
    if streaming:
        async with inkling.run_stream_events(
            "continue",
            conversation=loaded,
            conversation_address=ADDRESS,
            output_type=str,
            model=model,
        ) as stream:
            async for event in stream:
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
    else:
        result = await inkling.run(
            "continue",
            conversation=loaded,
            conversation_address=ADDRESS,
            output_type=str,
            model=model,
        )
    assert result is not None
    assert any(isinstance(part, CompactionPart) for part in received[0].parts)
    assert len(received) == 1
    assert result.usage.requests == first.usage.requests + 2
    restored = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    assert restored.usage == result.usage
    assert restored.revision == result.conversation.revision
    assert restored.new_message_index == len(result.all_messages())
    assert [m.parts for m in restored.messages] == [
        m.parts for m in result.all_messages()
    ]
    assert restored.messages[0].run_id is None
    assert restored.messages[0].compaction_id is not None
    archive = await inkling.conversations.history(conversation.id)
    assert [m.id for m in archive[:2]] == original_ids
    assert len(archive) == 4
    assert not any(isinstance(p, CompactionPart) for m in archive for p in m.parts)
    async with async_session() as session:
        [compaction] = await session.list(Compaction)
        assert compaction.trigger == "auto"
        assert compaction.membership_complete
        assert await session.count(CompactionMembership) == 6
    snapshot = await StepStore().latest_snapshot(run_id=result.run_id)
    assert snapshot is not None
    assert [m.parts for m in snapshot.messages] == [m.parts for m in restored.messages]

    summarizing.max_messages = 100
    received.clear()
    third = await inkling.run(
        "next turn",
        conversation=restored,
        conversation_address=ADDRESS,
        output_type=str,
        model=model,
    )
    assert any(isinstance(part, CompactionPart) for part in received[0].parts)
    assert not any(
        isinstance(p, UserPromptPart) and "old question" in str(p.content)
        for m in received
        for p in m.parts
    )
    assert third.usage.requests == result.usage.requests + 1


async def test_multiple_compactions_keep_independent_membership_and_tool_pairs(
    inkling: InklingTentacle,
    conversation: Conversation,
    summarizing: SummarizingCompaction[None],
) -> None:
    calls = 0
    observed: list[list[ModelMessage]] = []

    async def reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        nonlocal calls
        observed.append(list(messages))
        calls += 1
        if calls <= 3:
            yield {
                0: DeltaToolCall(
                    name="lookup", json_args="{}", tool_call_id=f"lookup-{calls}"
                )
            }
        else:
            yield "finished"

    async def lookup() -> str:
        return "large tool result " * 500

    result = await inkling.run(
        "long original request " * 200,
        conversation=conversation,
        conversation_address=ADDRESS,
        output_type=str,
        model=FunctionModel(stream_function=reply),
        toolsets=[FunctionToolset([lookup])],
    )
    async with async_session() as session:
        compactions = await session.list(Compaction, limit=None)
        assert len(compactions) == 3
        memberships = await session.list(CompactionMembership, limit=None)
        for previous, current in pairwise(compactions):
            after_ids = {
                m.model_message_id
                for m in memberships
                if m.compaction_id == previous.id and m.stage == "after"
            }
            before_ids = {
                m.model_message_id
                for m in memberships
                if m.compaction_id == current.id and m.stage == "before"
            }
            assert after_ids <= before_ids
        run = await session.get(AgentRun, result.run_id)
        assert run is not None
        assert len(run.messages) == 8
        assert all(m.run_id == result.run_id for m in run.messages)
    for request in observed:
        call_ids = {
            p.tool_call_id
            for m in request
            for p in m.parts
            if isinstance(p, ToolCallPart)
        }
        return_ids = {
            p.tool_call_id
            for m in request
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        }
        assert call_ids == return_ids
    restored = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    assert restored.usage == result.usage
    assert restored.usage.requests == 7
    assert [m.parts for m in restored.messages] == [
        m.parts for m in result.all_messages()
    ]


@pytest.mark.parametrize("outcome", ["success", "failure", "conflict"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_compacted_run_preserves_failures_and_revision_checks(
    inkling: InklingTentacle,
    conversation: Conversation,
    summarizing: SummarizingCompaction[None],
    outcome: str,
    streaming: bool,
) -> None:
    first = await inkling.run(
        "old question " * 200,
        conversation=conversation,
        conversation_address=ADDRESS,
        output_type=str,
        model=TestModel(call_tools=[], custom_output_text="old answer " * 200),
    )
    loaded = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )

    async def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert any(isinstance(p, CompactionPart) for m in messages for p in m.parts)
        if outcome == "failure":
            raise RuntimeError("model failed after compaction")
        if outcome == "conflict":
            current = await inkling.resolve_conversation(
                conversation.thread_id, conversation_address=ADDRESS
            )
            await ConversationStore(inkling.conversations).record_conversation(current)
        return ModelResponse(parts=[TextPart("continued")])

    async def stream_reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        await reply(messages, info)
        yield "continued"

    start = StartTurn[InklingOutput, None](user_prompt="continue")
    state = ReactState(conversation_address=ADDRESS, conversation=loaded)
    deps = ReactDeps(
        agent=inkling.agent,
        conversation_manager=inkling.conversations,
        agent_deps=None,
        capabilities=inkling.capabilities,
        model=FunctionModel(reply, stream_function=stream_reply),
        output_type=str,
    )
    expected = (
        pytest.raises(RuntimeError, match="model failed after compaction")
        if outcome == "failure"
        else pytest.raises(ConversationConflict)
        if outcome == "conflict"
        else nullcontext()
    )
    with expected:
        if streaming:
            async for _ in iter_react_graph_events(start, state=state, deps=deps):
                pass
        else:
            await build_react_graph(start).run(inputs=start, state=state, deps=deps)
    restored = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    assert any(
        isinstance(p, CompactionPart) for m in restored.messages for p in m.parts
    )
    archive = await inkling.conversations.history(conversation.id)
    assert len(archive) == (4 if outcome == "success" else 3)
    assert restored.usage.requests >= first.usage.requests + 1
    async with async_session() as session:
        assert await session.count(Compaction) == 1


async def test_rejected_summary_does_not_record_a_compaction(
    inkling: InklingTentacle,
    conversation: Conversation,
    summarizing: SummarizingCompaction[None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await inkling.run(
        "old question " * 20,
        conversation=conversation,
        conversation_address=ADDRESS,
        output_type=str,
        model=TestModel(call_tools=[], custom_output_text="old answer " * 20),
    )
    summarizing.model = TestModel(custom_output_text="a much larger summary " * 2000)
    record = AsyncMock(wraps=ConversationStore.record_compaction)
    monkeypatch.setattr(ConversationStore, "record_compaction", record)
    result = await inkling.run(
        "continue",
        conversation=await inkling.resolve_conversation(
            conversation.thread_id, conversation_address=ADDRESS
        ),
        conversation_address=ADDRESS,
        output_type=str,
        model=TestModel(call_tools=[], custom_output_text="continued"),
    )
    record.assert_not_awaited()
    assert result.usage.requests == first.usage.requests + 2
    assert not any(
        isinstance(p, CompactionPart) for m in result.all_messages() for p in m.parts
    )
    async with async_session() as session:
        assert await session.count(Compaction) == 0


async def test_compaction_archives_unsaved_input_before_replacing_it(
    inkling: InklingTentacle,
    conversation: Conversation,
    summarizing: SummarizingCompaction[None],
) -> None:
    first = await inkling.run(
        "old question " * 200,
        conversation=conversation,
        conversation_address=ADDRESS,
        output_type=str,
        model=TestModel(call_tools=[], custom_output_text="old answer " * 200),
    )
    loaded = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    pending = StoredModelResponse(
        run_id=first.run_id, parts=[TextPart("unsaved continuation")]
    )
    loaded.messages.append(pending)
    result = await inkling.run(
        "continue",
        conversation=loaded,
        conversation_address=ADDRESS,
        output_type=str,
        model=TestModel(call_tools=[], custom_output_text="continued"),
    )
    archive = await inkling.conversations.history(conversation.id)
    assert [m.parts for m in archive[:3]] == [m.parts for m in loaded.messages]
    assert len(archive) == 5
    restored = await inkling.resolve_conversation(
        conversation.thread_id, conversation_address=ADDRESS
    )
    assert [m.parts for m in restored.messages] == [
        m.parts for m in result.all_messages()
    ]
