from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace, TracebackType
from typing import ClassVar, Literal, cast

import pytest
from openai_codex import CodexConfig as CodexSdkConfig
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.client import ApprovalHandler
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    ItemCompletedNotification,
    MessagePhase,
    Personality,
    ReasoningEffort,
    ReasoningSummary,
    ReasoningSummaryValue,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnError,
    TurnItemsView,
    TurnStatus,
)
from openai_codex.models import Notification, NotificationPayload
from pydantic import BaseModel, SecretStr, TypeAdapter
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import PartStartEvent

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import CodexConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.agent.codex import CodexTentacle
from octomate.tentacles.agent.codex import base as codex_base
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.types.json import JsonObject
from tests.support.managers import (
    FakeConversation,
    FakeConversationManager,
    FakePresentedBatch,
)
from uuid_utils.compat import uuid7

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="private", chat_id="alice", user_id="alice"
)

HOOK_SECRET = SecretStr("test-hook-secret")
_THREAD = uuid7()
_ACCEPT_EDITS_THREAD = uuid7()
_BYPASS_THREAD = uuid7()


@dataclass
class ThreadCall:
    kind: Literal["start", "resume"]
    thread_id: str | None
    approval_mode: ApprovalMode | None
    base_instructions: str | None
    cwd: str | None
    developer_instructions: str | None
    ephemeral: bool | None
    model: str | None
    model_provider: str | None
    personality: Personality | None
    sandbox: Sandbox | None


@dataclass
class TurnCall:
    prompt: str
    approval_mode: ApprovalMode | None
    cwd: str | None
    effort: ReasoningEffort | None
    model: str | None
    output_schema: JsonObject | None
    personality: Personality | None
    sandbox: Sandbox | None
    summary: ReasoningSummary | None


def notification(method: str, payload: NotificationPayload) -> Notification:
    return Notification(method=method, payload=payload)


def text_script(text: str, *, thread_id: str = "thread-1") -> list[Notification]:
    return [
        notification(
            "item/agentMessage/delta",
            AgentMessageDeltaNotification(
                delta=text,
                item_id="msg-1",
                thread_id=thread_id,
                turn_id="turn-1",
            ),
        ),
        notification(
            "item/completed",
            ItemCompletedNotification(
                completed_at_ms=1,
                item=ThreadItem.model_validate(
                    {
                        "id": "msg-1",
                        "phase": MessagePhase.final_answer.value,
                        "text": text,
                        "type": "agentMessage",
                    }
                ),
                thread_id=thread_id,
                turn_id="turn-1",
            ),
        ),
        notification(
            "turn/completed",
            TurnCompletedNotification(
                thread_id=thread_id,
                turn=Turn(
                    id="turn-1",
                    items=[],
                    items_view=TurnItemsView.full,
                    status=TurnStatus.completed,
                ),
            ),
        ),
    ]


def failed_script(message: str, *, thread_id: str = "thread-1") -> list[Notification]:
    return [
        notification(
            "turn/completed",
            TurnCompletedNotification(
                thread_id=thread_id,
                turn=Turn(
                    id="turn-1",
                    error=TurnError(message=message),
                    items=[],
                    items_view=TurnItemsView.full,
                    status=TurnStatus.failed,
                ),
            ),
        )
    ]


class FakeTurn:
    def __init__(self) -> None:
        self.id = "turn-1"
        self.interrupted = False

    async def interrupt(self) -> None:
        self.interrupted = True

    async def stream(self) -> AsyncIterator[Notification]:
        for event in FakeCodex.script:
            yield event


class FakeThread:
    def __init__(self, id: str) -> None:
        self.id = id

    async def turn(
        self,
        input: str,
        *,
        approval_mode: ApprovalMode | None = None,
        cwd: str | None = None,
        effort: ReasoningEffort | None = None,
        model: str | None = None,
        output_schema: JsonObject | None = None,
        personality: Personality | None = None,
        sandbox: Sandbox | None = None,
        summary: ReasoningSummary | None = None,
    ) -> FakeTurn:
        turn = FakeTurn()
        FakeCodex.turns.append(turn)
        FakeCodex.turn_calls.append(
            TurnCall(
                prompt=input,
                approval_mode=approval_mode,
                cwd=cwd,
                effort=effort,
                model=model,
                output_schema=output_schema,
                personality=personality,
                sandbox=sandbox,
                summary=summary,
            )
        )
        return turn


class FakeCodex:
    script: ClassVar[list[Notification]] = []
    last_config: ClassVar[CodexSdkConfig | None] = None
    thread_calls: ClassVar[list[ThreadCall]] = []
    turn_calls: ClassVar[list[TurnCall]] = []
    turns: ClassVar[list[FakeTurn]] = []
    approval_handler: ClassVar[ApprovalHandler | None] = None
    approval_responses: ClassVar[list[JsonObject]] = []
    builds: ClassVar[int] = 0
    closed: ClassVar[int] = 0

    def __init__(self, config: CodexSdkConfig | None = None) -> None:
        FakeCodex.last_config = config
        FakeCodex.builds += 1

    async def __aenter__(self) -> FakeCodex:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        FakeCodex.closed += 1

    async def thread_start(
        self,
        *,
        approval_mode: ApprovalMode = ApprovalMode.auto_review,
        base_instructions: str | None = None,
        cwd: str | None = None,
        developer_instructions: str | None = None,
        ephemeral: bool | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        personality: Personality | None = None,
        sandbox: Sandbox | None = None,
    ) -> FakeThread:
        FakeCodex.thread_calls.append(
            ThreadCall(
                kind="start",
                thread_id=None,
                approval_mode=approval_mode,
                base_instructions=base_instructions,
                cwd=cwd,
                developer_instructions=developer_instructions,
                ephemeral=ephemeral,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=sandbox,
            )
        )
        return FakeThread("thread-new")

    async def thread_resume(
        self,
        thread_id: str,
        *,
        approval_mode: ApprovalMode | None = None,
        base_instructions: str | None = None,
        cwd: str | None = None,
        developer_instructions: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        personality: Personality | None = None,
        sandbox: Sandbox | None = None,
    ) -> FakeThread:
        FakeCodex.thread_calls.append(
            ThreadCall(
                kind="resume",
                thread_id=thread_id,
                approval_mode=approval_mode,
                base_instructions=base_instructions,
                cwd=cwd,
                developer_instructions=developer_instructions,
                ephemeral=None,
                model=model,
                model_provider=model_provider,
                personality=personality,
                sandbox=sandbox,
            )
        )
        return FakeThread(thread_id)


class ApprovalFakeTurn(FakeTurn):
    async def stream(self) -> AsyncIterator[Notification]:
        handler = FakeCodex.approval_handler
        assert handler is not None
        response = await asyncio.to_thread(
            handler,
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-new",
                "itemId": "cmd-1",
                "command": "pytest",
                "cwd": "/repo",
            },
        )
        FakeCodex.approval_responses.append(response)
        for event in FakeCodex.script:
            yield event


class ApprovalFakeThread(FakeThread):
    async def turn(
        self,
        input: str,
        *,
        approval_mode: ApprovalMode | None = None,
        cwd: str | None = None,
        effort: ReasoningEffort | None = None,
        model: str | None = None,
        output_schema: JsonObject | None = None,
        personality: Personality | None = None,
        sandbox: Sandbox | None = None,
        summary: ReasoningSummary | None = None,
    ) -> ApprovalFakeTurn:
        turn = ApprovalFakeTurn()
        FakeCodex.turns.append(turn)
        return turn


class ApprovalFakeCodex(FakeCodex):
    def __init__(self, config: CodexSdkConfig | None = None) -> None:
        super().__init__(config=config)
        # Mirror AsyncCodex's `_client._sync` slot so the tentacle can swap in a
        # handler-bearing sync client (patched below to capture the handler).
        self._client = SimpleNamespace(_sync=None)


@dataclass
class FakeFeelers:
    batch: FakePresentedBatch
    requests: list[object] = field(default_factory=list)

    async def present_actions(
        self, *, requests: object, **_: object
    ) -> FakePresentedBatch:
        self.requests.append(requests)
        return self.batch


@dataclass
class FakeChannel:
    feelers: FakeFeelers
    config: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(type="fake", agents=[])
    )


@dataclass
class RecordingDeferredActions:
    resolved: list[DeferredActionBatchResponse] = field(default_factory=list)
    marked: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def resolve_batch(self, awake: DeferredActionBatchResponse) -> None:
        self.resolved.append(awake)

    async def mark_batch(
        self, batch_id: uuid.UUID, status: str, *, completed: bool = False
    ) -> None:
        self.marked.append((batch_id, status))


class StructuredCodexResult(BaseModel):
    answer: str


def reset_fake_codex(script: list[Notification]) -> None:
    FakeCodex.script = script
    FakeCodex.last_config = None
    FakeCodex.thread_calls = []
    FakeCodex.turn_calls = []
    FakeCodex.turns = []
    FakeCodex.approval_handler = None
    FakeCodex.approval_responses = []
    FakeCodex.builds = 0
    FakeCodex.closed = 0


def _tentacle(
    conversations: FakeConversationManager,
    *,
    config: CodexConfig | None = None,
) -> CodexTentacle:
    return CodexTentacle(
        "codex",
        Octomate(conversations=conversations),
        config=config or CodexConfig(approval_mode="deny_all"),
        hook_secret=HOOK_SECRET,
    )


def codex_bridge_context(
    conversation: FakeConversation,
) -> codex_base.CodexBridgeContext:
    return codex_base.CodexBridgeContext(
        loop=asyncio.get_running_loop(),
        conversation=cast(codex_base.Conversation, conversation),
        conversation_address=KEY,
        run_name="react",
        session_allowed=set(conversation.allowed_tools),
        permission_mode=conversation.permission_mode,
    )


async def wait_for_pending(tentacle: CodexTentacle) -> uuid.UUID:
    for _ in range(10000):
        if tentacle.pending:
            return next(iter(tentacle.pending))
        await asyncio.sleep(0)
    raise AssertionError("no Codex deferred batch was parked")


async def test_run_stream_events_starts_thread_proxies_events_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    events = []
    async with tentacle:
        async with tentacle.run_stream_events(
            "fix it",
            conversation_address=KEY,
            thread_id=_THREAD,
            run_name="react",
            model="gpt-5.3-codex",
        ) as stream:
            async for event in stream:
                events.append(event)

    assert any(isinstance(event, PartStartEvent) for event in events)
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "done"

    [thread_call] = FakeCodex.thread_calls
    assert thread_call.kind == "start"
    assert thread_call.model == "gpt-5.3-codex"
    assert thread_call.approval_mode == ApprovalMode.deny_all
    assert thread_call.sandbox == Sandbox.workspace_write

    [recorded] = conversations.runs
    fake, _label, messages = recorded
    assert fake.external_id == "thread-1"
    assert messages


async def test_instructions_join_the_developer_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run-level instructions (an accomplice's framing included) ride the
    # thread's developer instructions, joined after the config default.
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    tentacle = _tentacle(
        conversations,
        config=CodexConfig(
            approval_mode="deny_all", developer_instructions="House style."
        ),
    )

    async with tentacle:
        await tentacle.run(
            "work the brief",
            conversation_address=KEY,
            thread_id=_THREAD,
            model="gpt-5.3-codex",
            instructions="You are an accomplice.",
        )

    [thread_call] = FakeCodex.thread_calls
    assert thread_call.developer_instructions == (
        "House style.\n\nYou are an accomplice."
    )


async def test_run_resumes_prior_thread_and_applies_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done", thread_id="prev-thread"))
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "codex", "")] = FakeConversation(
        external_id="prev-thread"
    )
    runtime = CodexSdkConfig(
        client_name="octomate-test", env={"EXISTING_RUNTIME_VALUE": "kept"}
    )
    tentacle = _tentacle(
        conversations,
        config=CodexConfig(
            cwd="/repo",
            runtime=runtime,
            approval_mode="auto_review",
            sandbox="read_only",
            base_instructions="base",
            developer_instructions="dev",
            ephemeral=True,
            model_provider="openai",
            personality="pragmatic",
            effort="xhigh",
            summary="detailed",
        ),
    )

    async with tentacle:
        result = await tentacle.run(
            "again",
            conversation_address=KEY,
            thread_id=_THREAD,
            model="gpt-5.5",
        )

    assert result.output == "done"
    assert FakeCodex.last_config is not None
    assert FakeCodex.last_config.client_name == runtime.client_name
    assert FakeCodex.last_config.env == {
        "EXISTING_RUNTIME_VALUE": "kept",
        "OCTOMATE_CODEX_DRIVEN": "1",
    }
    [thread_call] = FakeCodex.thread_calls
    assert thread_call.kind == "resume"
    assert thread_call.thread_id == "prev-thread"
    assert thread_call.cwd == "/repo"
    assert thread_call.approval_mode == ApprovalMode.auto_review
    assert thread_call.base_instructions == "base"
    assert thread_call.developer_instructions == "dev"
    assert thread_call.ephemeral is None
    assert thread_call.model == "gpt-5.5"
    assert thread_call.model_provider == "openai"
    assert thread_call.personality == Personality.pragmatic
    assert thread_call.sandbox == Sandbox.read_only

    [turn_call] = FakeCodex.turn_calls
    assert turn_call.cwd == "/repo"
    assert turn_call.effort == ReasoningEffort.xhigh
    assert turn_call.summary == ReasoningSummary(ReasoningSummaryValue.detailed)


async def test_run_with_output_type_passes_schema_and_validates_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script('{"answer":"ok"}'))
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        result = await tentacle.run(
            "structure this",
            conversation_address=KEY,
            thread_id=_THREAD,
            output_type=StructuredCodexResult,
        )

    assert result.output == StructuredCodexResult(answer="ok")
    [turn_call] = FakeCodex.turn_calls
    assert turn_call.output_schema == TypeAdapter(StructuredCodexResult).json_schema()


async def test_failed_run_persists_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(failed_script("boom"))
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    with pytest.raises(RuntimeError, match="boom"):
        async with tentacle:
            await tentacle.run(
                "break",
                conversation_address=KEY,
                thread_id=_THREAD,
            )

    [recorded] = conversations.runs
    fake, _label, messages = recorded
    assert fake.external_id == "thread-1"
    assert messages


async def test_user_approval_mode_bridges_sdk_requests_to_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_fake_codex(text_script("done"))

    def capture_handler(
        config: CodexSdkConfig | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> object:
        # Stands in for the sync CodexClient the tentacle swaps into `_client._sync`;
        # capture the per-thread approval handler the run binds.
        FakeCodex.approval_handler = approval_handler
        return object()

    async def start_fake_thread(
        self: CodexTentacle,
        client: codex_base.AsyncCodex,
        *,
        approval_mode: codex_base.CodexApprovalMode,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> ApprovalFakeThread:
        assert approval_mode == "user"
        assert isinstance(client, ApprovalFakeCodex)
        return ApprovalFakeThread("thread-new")

    monkeypatch.setattr(codex_base, "AsyncCodex", ApprovalFakeCodex)
    monkeypatch.setattr(codex_base, "CodexClient", capture_handler)
    monkeypatch.setattr(CodexTentacle, "start_codex_thread", start_fake_thread)
    approval = DeferredApproval(
        tool_name="codex_command_execution",
        tool_call_id="cmd-1",
        args=ApprovalRequest(tool_name="codex_command_execution"),
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    octomate = Octomate(
        conversations=FakeConversationManager(),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        channels=cast(dict[str, ChannelTentacle], {"im": FakeChannel(feelers=feelers)}),
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(approval_mode="user"),
        hook_secret=HOOK_SECRET,
    )
    octomate.connect(tentacle)

    async def drain() -> None:
        async with tentacle.run_stream_events(
            "run tests",
            conversation_address=KEY,
            thread_id=_THREAD,
        ) as stream:
            async for _event in stream:
                pass

    async with tentacle:
        task = asyncio.ensure_future(drain())
        batch_id = await wait_for_pending(tentacle)
        await octomate.kick(
            DeferredActionBatchResponse(
                batch_id=batch_id, approvals={approval.id: True}
            )
        )
        await task

    assert len(feelers.requests) == 1
    assert deferred_actions.resolved
    assert FakeCodex.approval_responses == [{"decision": "accept"}]


async def test_question_requests_bridge_to_cards() -> None:
    question = DeferredQuestion(
        tool_name="codex_user_input",
        tool_call_id="ask-1",
        args={"question": "Which branch?"},
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(questions=[question]))
    deferred_actions = RecordingDeferredActions()
    conversation = FakeConversation(thread_id=_THREAD)
    octomate = Octomate(
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        channels=cast(dict[str, ChannelTentacle], {"im": FakeChannel(feelers=feelers)}),
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(approval_mode="user"),
        hook_secret=HOOK_SECRET,
    )
    octomate.connect(tentacle)
    tentacle.bridge_contexts[_THREAD] = codex_bridge_context(conversation)

    task = asyncio.create_task(
        asyncio.to_thread(
            tentacle.handle_sdk_request,
            _THREAD,
            "mcp/elicitation/create",
            {
                "threadId": "thread-1",
                "requestId": "ask-1",
                "message": "Which branch?",
                "requestedSchema": {"properties": {"branch": {"type": "string"}}},
            },
        )
    )
    batch_id = await wait_for_pending(tentacle)
    await octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id,
            answers={question.id: "main"},
        )
    )
    response = await task

    assert len(feelers.requests) == 1
    assert deferred_actions.resolved
    assert response == {
        "action": "accept",
        "content": {"branch": "main"},
        "answer": "main",
    }


async def test_codex_approval_deny_and_timeout_paths() -> None:
    approval = DeferredApproval(
        tool_name="codex_command_execution",
        tool_call_id="cmd-1",
        args=ApprovalRequest(tool_name="codex_command_execution"),
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    conversation = FakeConversation(thread_id=_THREAD)
    octomate = Octomate(
        conversations=FakeConversationManager(),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        channels=cast(dict[str, ChannelTentacle], {"im": FakeChannel(feelers=feelers)}),
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(approval_mode="user"),
        hook_secret=HOOK_SECRET,
    )
    octomate.connect(tentacle)
    tentacle.bridge_contexts[_THREAD] = codex_bridge_context(conversation)

    task = asyncio.create_task(
        asyncio.to_thread(
            tentacle.handle_sdk_request,
            _THREAD,
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "itemId": "cmd-1", "command": "pytest"},
        )
    )
    batch_id = await wait_for_pending(tentacle)
    await octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id,
            approvals={approval.id: False},
        )
    )
    response = await task

    assert response["decision"] == "deny"
    assert "declined" in str(response["message"])

    timeout_tentacle = CodexTentacle(
        "codex-timeout",
        octomate,
        config=CodexConfig(approval_mode="user", approval_timeout=0.01),
        hook_secret=HOOK_SECRET,
    )
    octomate.connect(timeout_tentacle)
    timeout_tentacle.bridge_contexts[_THREAD] = codex_bridge_context(conversation)
    timeout_task = asyncio.create_task(
        asyncio.to_thread(
            timeout_tentacle.handle_sdk_request,
            _THREAD,
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "itemId": "cmd-1", "command": "pytest"},
        )
    )
    timeout_response = await timeout_task

    assert timeout_response["decision"] == "deny"
    assert "expired" in str(timeout_response["message"])
    assert deferred_actions.marked


async def test_codex_allow_session_and_permission_mode_auto_approval() -> None:
    approval = DeferredApproval(
        tool_name="codex_command_execution",
        tool_call_id="cmd-1",
        args=ApprovalRequest(tool_name="codex_command_execution"),
    )
    feelers = FakeFeelers(batch=FakePresentedBatch(approvals=[approval]))
    deferred_actions = RecordingDeferredActions()
    conversation = FakeConversation(thread_id=_THREAD)
    conversations = FakeConversationManager()
    octomate = Octomate(
        conversations=conversations,
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        channels=cast(dict[str, ChannelTentacle], {"im": FakeChannel(feelers=feelers)}),
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(approval_mode="user"),
        hook_secret=HOOK_SECRET,
    )
    octomate.connect(tentacle)
    tentacle.bridge_contexts[_THREAD] = codex_bridge_context(conversation)

    task = asyncio.create_task(
        asyncio.to_thread(
            tentacle.handle_sdk_request,
            _THREAD,
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "itemId": "cmd-1", "command": "pytest"},
        )
    )
    batch_id = await wait_for_pending(tentacle)
    await octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id,
            approvals={approval.id: True},
            allow_session=True,
        )
    )
    response = await task
    second_response = await asyncio.to_thread(
        tentacle.handle_sdk_request,
        _THREAD,
        "item/commandExecution/requestApproval",
        {"threadId": "thread-1", "itemId": "cmd-2", "command": "pytest"},
    )

    assert response == {"decision": "accept"}
    assert second_response == {"decision": "accept"}
    assert conversation.allowed_tools == ["codex_command_execution"]
    assert len(feelers.requests) == 1

    accept_edits = FakeConversation(
        thread_id=_ACCEPT_EDITS_THREAD,
        permission_mode="accept_edits",
    )
    bypass = FakeConversation(
        thread_id=_BYPASS_THREAD,
        permission_mode="bypass_permissions",
    )
    tentacle.bridge_contexts[_ACCEPT_EDITS_THREAD] = codex_bridge_context(accept_edits)
    tentacle.bridge_contexts[_BYPASS_THREAD] = codex_bridge_context(bypass)

    file_response = await asyncio.to_thread(
        tentacle.handle_sdk_request,
        _ACCEPT_EDITS_THREAD,
        "item/fileChange/requestApproval",
        {"threadId": "accept-edits", "itemId": "file-1"},
    )
    unknown_response = await asyncio.to_thread(
        tentacle.handle_sdk_request,
        _BYPASS_THREAD,
        "item/network/requestApproval",
        {"threadId": "bypass", "itemId": "net-1"},
    )

    assert file_response == {"decision": "accept"}
    assert unknown_response == {"decision": "accept"}
    assert len(feelers.requests) == 1


async def test_shutdown_interrupts_live_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex([])
    tentacle = _tentacle(FakeConversationManager())
    turn = FakeTurn()
    tentacle.live_turns[uuid.uuid4()] = cast(codex_base.AsyncTurnHandle, turn)

    await tentacle.__aexit__()

    assert turn.interrupted
    assert not tentacle.live_turns


async def test_pool_reuses_client_per_thread_and_drains_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    other_thread = uuid7()
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        await tentacle.run("one", conversation_address=KEY, thread_id=_THREAD)
        await tentacle.run("two", conversation_address=KEY, thread_id=_THREAD)
        # Same thread: one warm client, one thread_start, both turns reuse it.
        assert FakeCodex.builds == 1
        assert len([c for c in FakeCodex.thread_calls if c.kind == "start"]) == 1
        assert len(FakeCodex.turn_calls) == 2

        await tentacle.run(
            "elsewhere", conversation_address=KEY, thread_id=other_thread
        )
        # A different thread gets its own client and its own thread_start.
        assert FakeCodex.builds == 2

    # Exiting the tentacle drains the pool, closing every warm client.
    assert FakeCodex.closed == 2
    assert tentacle.pool is None
