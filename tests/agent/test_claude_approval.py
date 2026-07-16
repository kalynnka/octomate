"""Phase 4 — the Claude approval/question bridge.

A live Claude session blocks on `can_use_tool` (tool approvals) or a PreToolUse
`AskUserQuestion` hook (questions); the tentacle presents cards through the
channel, parks a future, and resolves it when the human response reaches
`Octomate.kick`. These tests drive the run in a task and deliver the response
out-of-band, the way a card button click would.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookInput,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import Message, ToolPermissionContext
from pydantic_ai.tools import DeferredToolRequests

from pydantic import SecretStr

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import ClaudeCodeConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.agent.claude import ClaudeCodeTentacle
from octomate.tentacles.agent.claude import base as claude_base
from octomate.tentacles.channel.base import ChannelTentacle
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


class ScriptedClaudeClient:
    """Configurable `ClaudeSDKClient` stand-in. `mode="approval"` drives one
    gated Bash call through `can_use_tool`; `mode="question"` drives the
    AskUserQuestion PreToolUse hook. The SDK's decision is recorded for assertion."""

    mode: str = "approval"
    last_options: ClaudeAgentOptions | None = None
    decisions: list[object] = []
    # Option labels the AskUserQuestion hook is fed; overridden to exercise
    # truncation when a question offers more than the choice cap.
    question_option_labels: list[str] = ["A", "B"]

    def __init__(
        self, options: ClaudeAgentOptions | None = None, transport: object = None
    ) -> None:
        ScriptedClaudeClient.last_options = options
        ScriptedClaudeClient.decisions = []
        self.options = options

    async def __aenter__(self) -> ScriptedClaudeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[Message]:
        assert self.options is not None
        if ScriptedClaudeClient.mode in ("approval", "approval_twice"):
            can_use_tool = self.options.can_use_tool
            assert can_use_tool is not None
            calls = 2 if ScriptedClaudeClient.mode == "approval_twice" else 1
            for index in range(calls):
                tool_use_id = f"t{index + 1}"
                yield AssistantMessage(
                    content=[
                        ToolUseBlock(
                            id=tool_use_id, name="Bash", input={"command": "ls"}
                        )
                    ],
                    model="m",
                )
                result = await can_use_tool(
                    "Bash",
                    {"command": "ls"},
                    ToolPermissionContext(tool_use_id=tool_use_id),
                )
                ScriptedClaudeClient.decisions.append(result)
                denied = isinstance(result, PermissionResultDeny)
                yield UserMessage(
                    content=[
                        ToolResultBlock(
                            tool_use_id=tool_use_id, content="x", is_error=denied
                        )
                    ]
                )
        else:
            assert self.options.hooks is not None
            hook = self.options.hooks["PreToolUse"][0].hooks[0]
            hook_input = cast(
                HookInput,
                {
                    "tool_input": {
                        "questions": [
                            {
                                "question": "Pick one",
                                "options": [
                                    {"label": label, "description": ""}
                                    for label in self.question_option_labels
                                ],
                            }
                        ]
                    }
                },
            )
            output = await hook(hook_input, "q1", cast(HookContext, {}))
            ScriptedClaudeClient.decisions.append(output)
        yield AssistantMessage(content=[TextBlock(text="done")], model="m")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="done",
        )


def _build(
    batch: FakePresentedBatch,
    *,
    config: ClaudeCodeConfig | None = None,
    conversation: FakeConversation | None = None,
) -> tuple[ClaudeCodeTentacle, RecordingDeferredActions, FakeFeelers]:
    feelers = FakeFeelers(batch=batch)
    dam = RecordingDeferredActions()
    conversations = FakeConversationManager()
    if conversation is not None:
        conversations.store[(_THREAD, "claude")] = conversation
    octomate = Octomate(
        conversations=conversations,
        deferred_actions=cast(DeferredActionManager, dam),
        channels=cast(dict[str, ChannelTentacle], {"im": FakeChannel(feelers=feelers)}),
    )
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=config or ClaudeCodeConfig(),
        hook_secret=HOOK_SECRET,
    )
    octomate.connect(tentacle)
    return tentacle, dam, feelers


def _conversation(tentacle: ClaudeCodeTentacle) -> FakeConversation:
    convs = cast(FakeConversationManager, tentacle.octomate.conversations)
    return convs.store[(_THREAD, "claude")]


async def _drain(tentacle: ClaudeCodeTentacle) -> None:
    async with tentacle.run_stream_events(
        "do it", conversation_address=KEY, thread_id=_THREAD, run_name="react"
    ) as stream:
        async for _event in stream:
            pass


async def _wait_for_pending(tentacle: ClaudeCodeTentacle) -> uuid.UUID:
    for _ in range(10000):
        if tentacle.pending:
            return next(iter(tentacle.pending))
        await asyncio.sleep(0)
    raise AssertionError("no deferred batch was parked")


async def test_approval_allow_lets_the_tool_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "approval")
    approval = DeferredApproval(
        tool_name="Bash", tool_call_id="t1", args=ApprovalRequest(tool_name="Bash")
    )
    tentacle, dam, feelers = _build(FakePresentedBatch(approvals=[approval]))

    task = asyncio.ensure_future(_drain(tentacle))
    batch_id = await _wait_for_pending(tentacle)
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, approvals={approval.id: True})
    )
    await task

    assert len(feelers.requests) == 1
    assert dam.resolved  # the batch was persisted on resolution
    assert isinstance(ScriptedClaudeClient.decisions[0], PermissionResultAllow)


async def test_approval_deny_feeds_reason_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "approval")
    approval = DeferredApproval(
        tool_name="Bash", tool_call_id="t1", args=ApprovalRequest(tool_name="Bash")
    )
    tentacle, _dam, _feelers = _build(FakePresentedBatch(approvals=[approval]))

    task = asyncio.ensure_future(_drain(tentacle))
    batch_id = await _wait_for_pending(tentacle)
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, approvals={approval.id: False})
    )
    await task

    decision = ScriptedClaudeClient.decisions[0]
    assert isinstance(decision, PermissionResultDeny)
    assert "Bash" in decision.message


async def test_allow_session_suppresses_repeat_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "approval_twice")
    approval = DeferredApproval(
        tool_name="Bash", tool_call_id="t1", args=ApprovalRequest(tool_name="Bash")
    )
    tentacle, _dam, feelers = _build(FakePresentedBatch(approvals=[approval]))

    task = asyncio.ensure_future(_drain(tentacle))
    batch_id = await _wait_for_pending(tentacle)
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id, approvals={approval.id: True}, allow_session=True
        )
    )
    await task

    # The first Bash call raised a card; the second was auto-approved for the
    # session, so only one batch was ever presented.
    assert len(feelers.requests) == 1
    assert len(ScriptedClaudeClient.decisions) == 2
    assert all(
        isinstance(d, PermissionResultAllow) for d in ScriptedClaudeClient.decisions
    )
    # The grant was persisted on the conversation for future turns.
    assert _conversation(tentacle).allowed_tools == ["Bash"]


async def test_persisted_allowed_tool_skips_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "approval")
    seeded = FakeConversation(permission_mode="default", allowed_tools=["Bash"])
    tentacle, _dam, feelers = _build(FakePresentedBatch(), conversation=seeded)

    await _drain(tentacle)

    # Bash was pre-granted for the conversation, so no card was presented.
    assert feelers.requests == []
    assert isinstance(ScriptedClaudeClient.decisions[0], PermissionResultAllow)


async def test_permission_mode_drives_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "question")  # no can_use_tool
    seeded = FakeConversation(permission_mode="accept_edits")
    question = DeferredQuestion(
        tool_name="AskUserQuestion",
        tool_call_id="q1",
        position=0,
        args={"question": "Pick one", "choices": ["A"], "hint": "choose"},
    )
    tentacle, _dam, _feelers = _build(
        FakePresentedBatch(questions=[question]), conversation=seeded
    )

    task = asyncio.ensure_future(_drain(tentacle))
    batch_id = await _wait_for_pending(tentacle)
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, answers={question.id: "A"})
    )
    await task

    options = ScriptedClaudeClient.last_options
    assert options is not None and options.permission_mode == "acceptEdits"


async def test_approval_timeout_denies_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "approval")
    approval = DeferredApproval(
        tool_name="Bash", tool_call_id="t1", args=ApprovalRequest(tool_name="Bash")
    )
    tentacle, _dam, _feelers = _build(
        FakePresentedBatch(approvals=[approval]),
        config=ClaudeCodeConfig(approval_timeout=0.01),
    )

    # No one ever answers; the wait times out and the tool is denied.
    await _drain(tentacle)

    decision = ScriptedClaudeClient.decisions[0]
    assert isinstance(decision, PermissionResultDeny)
    assert "expired" in decision.message
    assert not tentacle.pending  # the parked future was cleaned up


async def test_ask_user_question_hook_feeds_answer_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "question")
    question = DeferredQuestion(
        tool_name="AskUserQuestion",
        tool_call_id="q1",
        position=0,
        args={"question": "Pick one", "choices": ["A", "B"], "hint": "choose"},
    )
    tentacle, _dam, feelers = _build(FakePresentedBatch(questions=[question]))

    task = asyncio.ensure_future(_drain(tentacle))
    batch_id = await _wait_for_pending(tentacle)
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, answers={question.id: "A"})
    )
    await task

    assert feelers.requests  # a question batch was presented
    output = ScriptedClaudeClient.decisions[0]
    reason = cast(dict[str, dict[str, str]], output)["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert "A" in reason and "Pick one" in reason


async def test_ask_user_question_truncates_choices_to_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claude's AskUserQuestion may offer up to 4 options; octomate caps a question's
    # choices, so the card presents (no validation crash) with only the cap kept.
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", ScriptedClaudeClient)
    monkeypatch.setattr(ScriptedClaudeClient, "mode", "question")
    monkeypatch.setattr(
        ScriptedClaudeClient, "question_option_labels", ["A", "B", "C", "D"]
    )
    question = DeferredQuestion(
        tool_name="AskUserQuestion",
        tool_call_id="q1",
        position=0,
        args={"question": "Pick one", "choices": ["A", "B", "C"], "hint": ""},
    )
    tentacle, _dam, feelers = _build(FakePresentedBatch(questions=[question]))

    task = asyncio.ensure_future(_drain(tentacle))
    batch_id = await _wait_for_pending(tentacle)
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch_id, answers={question.id: "A"})
    )
    await task

    [presented] = feelers.requests
    args = cast(DeferredToolRequests, presented).calls[0].args_as_dict()
    choices = args["questions"][0]["choices"]
    assert choices == ["A", "B", "C"]
    assert len(choices) == MAX_QUESTION_CHOICES


async def test_kick_routes_response_to_live_waiter() -> None:
    tentacle, _dam, _feelers = _build(FakePresentedBatch())

    batch_id = uuid.uuid4()
    future: asyncio.Future[DeferredActionBatchResponse] = (
        asyncio.get_running_loop().create_future()
    )
    tentacle.pending[batch_id] = future

    response = DeferredActionBatchResponse(batch_id=batch_id, approvals={})
    await tentacle.octomate.kick(response)

    assert future.done() and future.result() is response
