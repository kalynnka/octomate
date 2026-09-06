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
    ApprovalsReviewer,
    Config,
    ItemCompletedNotification,
    MessagePhase,
    Personality,
    ReasoningEffort,
    ReasoningSummary,
    ReasoningSummaryValue,
    SandboxWorkspaceWrite,
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
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import AgentModelConfig, ChannelConfig, OctomateConfig
from octomate.config.agents import CodexConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import OctomateSession
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import TeleportDecision
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex import base as codex_base
from octomate.tentacles.feelers.base import Feelers
from octomate.types.json import JsonObject
from octomate.types.permissions import CodexPermissionMode
from tests.support.agents import CODEX_MODELS
from tests.support.channels import FakeChannelTentacle
from tests.support.config import registered
from tests.support.managers import (
    FakeConversation,
    FakeConversationManager,
    FakePresentedBatch,
    RecordingSuspender,
)

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)

HOOK_SECRET = SecretStr("test-hook-secret")
_THREAD = uuid7()


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
        # Mirror AsyncCodex's `_client._sync` slot: the tentacle swaps a
        # handler-bearing sync client into it for every client it builds.
        self._client = SimpleNamespace(_sync=None)

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


@dataclass
class FakeFeelers:
    batch: FakePresentedBatch
    requests: list[object] = field(default_factory=list)

    async def present_actions(
        self, *, requests: object, **_: object
    ) -> FakePresentedBatch:
        self.requests.append(requests)
        return self.batch


def a_channel(feelers: FakeFeelers) -> FakeChannelTentacle:
    """The `im` channel the tentacle presents approvals and questions through,
    its feelers recording what was asked."""
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake", agents=[AgentModelConfig(agent="inkling", model="test")]
        )
    )
    channel.feelers = cast(Feelers, feelers)
    return channel


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
        config=config
        or CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
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
    # The configured preset, in a chat thread as in any other: the run has a
    # workspace of its own for it to be scoped to.
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
            models=set(CODEX_MODELS),
            permission_mode="deny_all",
            developer_instructions="House style.",
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
        thread_id=_THREAD, external_id="prev-thread"
    )
    runtime = CodexSdkConfig(
        client_name="octomate-test", env={"EXISTING_RUNTIME_VALUE": "kept"}
    )
    tentacle = _tentacle(
        conversations,
        config=CodexConfig(
            models=set(CODEX_MODELS),
            runtime=runtime,
            permission_mode="auto_review",
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
    # The configured `cwd` is not where a projectless thread lands any more.
    assert thread_call.cwd == str(tentacle.octomate.workspaces.open(_THREAD, None).path)
    assert thread_call.approval_mode == ApprovalMode.auto_review
    assert thread_call.base_instructions == "base"
    assert thread_call.developer_instructions == "dev"
    assert thread_call.ephemeral is None
    assert thread_call.model == "gpt-5.5"
    assert thread_call.model_provider == "openai"
    assert thread_call.personality == Personality.pragmatic
    assert thread_call.sandbox == Sandbox.workspace_write

    [turn_call] = FakeCodex.turn_calls
    assert turn_call.cwd == str(tentacle.octomate.workspaces.open(_THREAD, None).path)
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
        plan: codex_base.CodexPermissionPlan,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> ApprovalFakeThread:
        # `user_review` is the one posture with no public SDK knob, so it is the
        # reviewer path the bridge exists for.
        assert plan.sdk_mode is None
        assert plan.reviewer is codex_base.ApprovalsReviewer.user
        assert isinstance(client, FakeCodex)
        return ApprovalFakeThread("thread-new")

    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
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
        config=registered(HOOK_SECRET.get_secret_value()),
        conversations=FakeConversationManager(),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        tentacles={"im": a_channel(feelers)},
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="user_review"),
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
        config=registered(HOOK_SECRET.get_secret_value()),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        tentacles={"im": a_channel(feelers)},
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="user_review"),
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
        config=registered(HOOK_SECRET.get_secret_value()),
        conversations=FakeConversationManager(),
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        tentacles={"im": a_channel(feelers)},
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="user_review"),
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
        config=CodexConfig(
            models=set(CODEX_MODELS),
            permission_mode="user_review",
            approval_timeout=0.01,
        ),
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


async def test_codex_allow_session_auto_approves_the_next_request() -> None:
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
        config=registered(HOOK_SECRET.get_secret_value()),
        conversations=conversations,
        deferred_actions=cast(DeferredActionManager, deferred_actions),
        tentacles={"im": a_channel(feelers)},
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="user_review"),
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

    # The grant is the only thing the bridge short-circuits on now: a posture that
    # forgoes review says so to the SDK, which then sends no request at all.
    assert response == {"decision": "accept"}
    assert second_response == {"decision": "accept"}
    assert conversation.allowed_tools == ["codex_command_execution"]
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


@pytest.mark.parametrize(
    ("permission_mode", "sdk_mode", "reviewer"),
    [
        ("user_review", None, ApprovalsReviewer.user),
        ("auto_review", ApprovalMode.auto_review, ApprovalsReviewer.auto_review),
        ("deny_all", ApprovalMode.deny_all, None),
    ],
)
def test_every_posture_names_its_row_of_the_sdk_approval_knobs(
    permission_mode: CodexPermissionMode,
    sdk_mode: ApprovalMode | None,
    reviewer: ApprovalsReviewer | None,
) -> None:
    plan = codex_base.CODEX_PERMISSION_PLANS[permission_mode]
    assert (plan.sdk_mode, plan.reviewer) == (sdk_mode, reviewer)


async def test_the_conversations_posture_overrides_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "codex", "")] = FakeConversation(
        thread_id=_THREAD, permission_mode="auto_review"
    )
    tentacle = _tentacle(
        conversations,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    [thread_call] = FakeCodex.thread_calls
    assert thread_call.approval_mode == ApprovalMode.auto_review


async def test_the_sandbox_is_the_operators_and_no_posture_moves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sandbox is what a command may touch when nobody is asked, so it is fixed
    for the run: a conversation's approval posture never rewrites it."""
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "codex", "")] = FakeConversation(
        thread_id=_THREAD, permission_mode="deny_all"
    )
    tentacle = _tentacle(
        conversations,
        config=CodexConfig(
            models=set(CODEX_MODELS), permission_mode="auto_review", sandbox="read_only"
        ),
    )

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    [thread_call] = FakeCodex.thread_calls
    assert thread_call.approval_mode == ApprovalMode.deny_all
    assert thread_call.sandbox == Sandbox.read_only


async def test_a_driven_run_opens_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`workspace_write` ships `networkAccess: false`, which leaves a coding agent
    with no registry, no API and no remote. Two things have to hold for the network
    to be open, so both are asserted here: the app-server is told to resolve the
    preset with it on, and the turn sends no policy of its own to stamp it back off.
    """
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    assert FakeCodex.last_config is not None
    assert FakeCodex.last_config.config_overrides == (
        codex_base.NETWORK_ACCESS,
        "mcp_servers.octomate.enabled=false",
    )
    [turn_call] = FakeCodex.turn_calls
    assert turn_call.sandbox is None
    # And the write scope is untouched: the thread still carries the preset.
    [thread_call] = FakeCodex.thread_calls
    assert thread_call.sandbox == Sandbox.workspace_write


async def test_an_operators_own_network_answer_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex keeps the last `--config` it is handed, so a deployment that has said
    something about the key itself has to be the one that lands."""
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    operator = "sandbox_workspace_write.network_access=false"
    tentacle = _tentacle(
        FakeConversationManager(),
        config=CodexConfig(
            models=set(CODEX_MODELS),
            permission_mode="deny_all",
            runtime=CodexSdkConfig(config_overrides=(operator,)),
        ),
    )

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    assert FakeCodex.last_config is not None
    assert FakeCodex.last_config.config_overrides == (
        codex_base.NETWORK_ACCESS,
        operator,
        "mcp_servers.octomate.enabled=false",
    )


def test_the_network_override_names_a_key_the_sdk_still_has() -> None:
    """The override is a config string, so nothing type-checks it and a renamed key
    would fail silently in the safe-looking direction — the network simply staying
    shut. The SDK ships the config schema it is a path into, so the key is pinned
    against that here rather than left to a live run to disprove."""
    key, _, value = codex_base.NETWORK_ACCESS.partition("=")
    table, _, field = key.rpartition(".")

    assert table in Config.model_fields
    assert Config.model_fields[table].annotation == SandboxWorkspaceWrite | None
    assert field in SandboxWorkspaceWrite.model_fields
    assert SandboxWorkspaceWrite.model_fields[field].annotation == bool | None
    # Off by default is the whole reason this exists.
    assert SandboxWorkspaceWrite.model_fields[field].default is False
    assert value == "true"


async def test_a_claude_posture_on_a_codex_conversation_falls_back_to_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The column holds every provider's scale, so the tentacle establishes the arm is
    its own. `Conversation` already refuses this pairing; this is the second edge."""
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "codex", "")] = FakeConversation(
        thread_id=_THREAD, permission_mode="bypassPermissions"
    )
    tentacle = _tentacle(
        conversations,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    [thread_call] = FakeCodex.thread_calls
    assert thread_call.approval_mode == ApprovalMode.deny_all


@pytest.mark.parametrize(
    "permission_mode",
    # Only the posture that needs a human falls back when there is none; one that
    # already says who reviews is left alone.
    ["user_review", "auto_review", "deny_all"],
)
async def test_a_subagent_run_declines_only_where_a_human_was_needed(
    monkeypatch: pytest.MonkeyPatch,
    permission_mode: CodexPermissionMode,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    conversation = FakeConversation(
        thread_id=_THREAD, agent_tentacle_id="codex", permission_mode=permission_mode
    )
    conversations.store[(_THREAD, "codex", "")] = conversation
    tentacle = _tentacle(conversations)

    async with tentacle:
        await tentacle.subagent_run(
            "audit it",
            conversation_address=KEY,
            thread_id=_THREAD,
            conversation_id=conversation.id,
            run_name="audit",
        )

    [thread_call] = FakeCodex.thread_calls
    expected = (
        ApprovalMode.deny_all
        if permission_mode == "user_review"
        else codex_base.CODEX_PERMISSION_PLANS[permission_mode].sdk_mode
    )
    assert thread_call.approval_mode == expected


def a_kicker(octomate: Octomate, secret: str = "lu-token") -> UserProfile:
    """`lu`, registered with `secret` on their row, as the sender profile the
    driven turn's session carries — cached so the bearer resolves without a
    database."""
    lu = User(username="lu", name="lu", secret=secret)
    octomate.users.cache_user(lu)
    return UserProfile(channel_tentacle_id="im", channel_user_id="alice", user_id=lu.id)


async def test_a_registered_octomate_session_wires_the_launch_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    octomate = Octomate(
        conversations=conversations,
        config=OctomateConfig(port=8123),
    )
    conversation = await conversations.ensure(_THREAD, agent_tentacle_id="codex")
    octomate.gateway.register(
        OctomateSession(
            channel_routes={},
            current_agent_id="codex",
            conversation_id=conversation.id,
            user_profile=a_kicker(octomate),
        )
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    config = FakeCodex.last_config
    assert config is not None
    assert config.env is not None
    # The kicker's own credential: the turn speaks as the human it represents.
    assert config.env[codex_base.GATEWAY_TOKEN_ENV] == "lu-token"

    assert config.env[codex_base.GATEWAY_CONVERSATION_ENV] == str(conversation.id)
    assert config.env[codex_base.DRIVEN_ENV] == "1"
    assert config.config_overrides == (
        codex_base.NETWORK_ACCESS,
        "mcp_servers.octomate.enabled=true",
        "mcp_servers.octomate.url=http://127.0.0.1:8123/octomate/mcp",
        "mcp_servers.octomate.bearer_token_env_var=OCTOMATE_GATEWAY_TOKEN",
        # The native entry's own Authorization would outrank the bearer above.
        "mcp_servers.octomate.http_headers={}",
        "mcp_servers.octomate.env_http_headers="
        '{"X-Octomate-Conversation" = "OCTOMATE_GATEWAY_CONVERSATION"}',
    )


async def test_a_turn_without_a_octomate_session_launches_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clean means the server is turned off, not merely left unmentioned: the
    # operator's `~/.codex/config.toml` may hold a native gateway entry with
    # their own credential in it, and an app-server is a child of this host.
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    tentacle = _tentacle(FakeConversationManager())

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    config = FakeCodex.last_config
    assert config is not None
    assert config.config_overrides == (
        codex_base.NETWORK_ACCESS,
        "mcp_servers.octomate.enabled=false",
    )
    assert codex_base.GATEWAY_TOKEN_ENV not in (config.env or {})


async def test_a_turn_kicked_by_an_unregistered_user_launches_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The session is registered, but its kicker is a visitor: no credential to
    # speak with, so the spells are withheld rather than borrowed.
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    octomate = Octomate(
        conversations=conversations,
        config=OctomateConfig(port=8123),
    )
    conversation = await conversations.ensure(_THREAD, agent_tentacle_id="codex")
    octomate.gateway.register(
        OctomateSession(
            channel_routes={},
            current_agent_id="codex",
            conversation_id=conversation.id,
            user_profile=UserProfile(
                channel_tentacle_id="im", channel_user_id="visitor"
            ),
        )
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )

    async with tentacle:
        await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)

    config = FakeCodex.last_config
    assert config is not None
    assert config.config_overrides == (
        codex_base.NETWORK_ACCESS,
        "mcp_servers.octomate.enabled=false",
    )
    assert codex_base.GATEWAY_TOKEN_ENV not in (config.env or {})


async def test_a_gateway_wiring_flip_evicts_the_pooled_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    octomate = Octomate(
        conversations=conversations,
        config=OctomateConfig(port=8123),
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )

    async with tentacle:
        await tentacle.run("one", conversation_address=KEY, thread_id=_THREAD)
        assert (FakeCodex.builds, FakeCodex.closed) == (1, 0)

        conversation = await conversations.ensure(_THREAD, agent_tentacle_id="codex")
        session = OctomateSession(
            channel_routes={},
            current_agent_id="codex",
            conversation_id=conversation.id,
            user_profile=a_kicker(octomate),
        )
        octomate.gateway.register(session)
        await tentacle.run("two", conversation_address=KEY, thread_id=_THREAD)
        assert (FakeCodex.builds, FakeCodex.closed) == (2, 1)

        # And back again once the turn no longer has the gateway.
        octomate.gateway.unregister(session)
        await tentacle.run("three", conversation_address=KEY, thread_id=_THREAD)
        assert (FakeCodex.builds, FakeCodex.closed) == (3, 2)


async def test_a_registered_gateway_without_a_served_endpoint_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    octomate = Octomate(conversations=conversations)
    conversation = await conversations.ensure(_THREAD, agent_tentacle_id="codex")
    octomate.gateway.register(
        OctomateSession(
            channel_routes={},
            current_agent_id="codex",
            conversation_id=conversation.id,
            user_profile=a_kicker(octomate),
        )
    )
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )

    async with tentacle:
        with pytest.raises(RuntimeError, match="cannot name the served MCP endpoint"):
            await tentacle.run("fix it", conversation_address=KEY, thread_id=_THREAD)


class BindingFakeTurn(FakeTurn):
    """A turn whose stream casts `teleport` into a project after its first
    notification — the session records the decision the way the served tool does
    — and then runs out, as an interrupted app-server turn does."""

    session: ClassVar[OctomateSession | None] = None

    async def stream(self) -> AsyncIterator[Notification]:
        first, *rest = FakeCodex.script
        yield first
        assert BindingFakeTurn.session is not None
        BindingFakeTurn.session.decision = TeleportDecision(
            hint="into inky", here=True, project="inky"
        )
        for event in rest:
            yield event


class BindingFakeThread(FakeThread):
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
    ) -> BindingFakeTurn:
        turn = BindingFakeTurn()
        FakeCodex.turns.append(turn)
        return turn


async def test_a_teleport_mid_turn_interrupts_it_and_ends_it_as_a_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("working"))
    conversations = FakeConversationManager()
    octomate = Octomate(
        conversations=conversations,
        config=OctomateConfig(port=8123),
    )
    conversation = await conversations.ensure(_THREAD, agent_tentacle_id="codex")
    session = OctomateSession(
        channel_routes={},
        current_agent_id="codex",
        conversation_id=conversation.id,
        user_profile=a_kicker(octomate),
    )
    octomate.gateway.register(session)
    BindingFakeTurn.session = session

    async def start_binding_thread(
        self: CodexTentacle,
        client: codex_base.AsyncCodex,
        *,
        plan: codex_base.CodexPermissionPlan,
        base_instructions: str | None,
        cwd: str | None,
        developer_instructions: str | None,
        ephemeral: bool | None,
        model: str | None,
        model_provider: str | None,
        personality: Personality | None,
        sandbox: Sandbox,
    ) -> BindingFakeThread:
        return BindingFakeThread("thread-new")

    monkeypatch.setattr(CodexTentacle, "start_codex_thread", start_binding_thread)
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
    )
    suspender = RecordingSuspender()

    events = []
    async with tentacle:
        async with tentacle.run_stream_events(
            "work on inky",
            conversation_address=KEY,
            thread_id=_THREAD,
            run_name="react",
            deferred_suspender=suspender,
        ) as stream:
            async for event in stream:
                events.append(event)

    [turn] = FakeCodex.turns
    assert turn.interrupted
    assert isinstance(events[-1], AgentRunResultEvent)
    output = events[-1].result.output
    assert isinstance(output, DeferredToolRequests)
    [call] = output.calls
    assert call.tool_name == "teleport"
    assert output.metadata[call.tool_call_id]["kind"] == "teleport"
    assert output.metadata[call.tool_call_id]["project"] == "inky"
    # Suspended through the one entry the graph resumes from.
    assert suspender.suspended == [output]


async def test_a_resumed_turn_opens_from_what_the_graph_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "codex", "")] = FakeConversation(
        external_id="thread-prev"
    )
    tentacle = _tentacle(conversations)
    results = DeferredToolResults(
        calls={"call_bind": "This thread is about 'inky' now. Carry on."}
    )

    async with tentacle:
        result = await tentacle.run(
            None,
            conversation_address=KEY,
            thread_id=_THREAD,
            deferred_tool_results=results,
        )

    assert result.output == "done"
    # The app-server takes no tool result back: the resolution is its next prompt,
    # on the thread it already has.
    [turn_call] = FakeCodex.turn_calls
    assert turn_call.prompt == "This thread is about 'inky' now. Carry on."
    [thread_call] = FakeCodex.thread_calls
    assert thread_call.kind == "resume"
    assert thread_call.thread_id == "thread-prev"
