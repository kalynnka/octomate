from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import ClassVar, Literal, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookInput,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
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
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import Claim, ClaudeCodeConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import SummonDecision
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.claude import base as claude_base
from octomate.tentacles.agents.claude.adapter import ClaudeRunAccumulator
from tests.support.agents import CLAUDE_MODELS
from tests.support.managers import FakeConversation, FakeConversationManager

SummonDecisionAdapter = TypeAdapter(SummonDecision)

_SUMMON_OUTPUT = {
    "action": "summon",
    "reason": "needs the coder",
    "agent_id": "inkling",
    "model": "opus",
    "hint": "help with code",
    "summon": "please take over",
}

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)


_THREAD = uuid7()


class FakeClaudeClient:
    """Stands in for `ClaudeSDKClient`: captures the options + prompt and yields
    a scripted Claude message stream (text, a tool round-trip, a final answer)."""

    last_options: object = None
    last_prompt: str | None = None
    last_transport: object = None

    def __init__(self, options: object = None, transport: object = None) -> None:
        FakeClaudeClient.last_options = options
        FakeClaudeClient.last_transport = transport

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


def _tentacle(
    conversations: FakeConversationManager,
    *,
    config: ClaudeCodeConfig | None = None,
) -> ClaudeCodeTentacle:
    return ClaudeCodeTentacle(
        "claude",
        Octomate(conversations=conversations),
        config=config or ClaudeCodeConfig(models=set(CLAUDE_MODELS)),
    )


async def test_run_stream_events_proxies_events_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    events = []
    async with tentacle.run_stream_events(
        "fix it", conversation_address=KEY, thread_id=_THREAD, run_name="react"
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


async def test_a_run_addressed_by_conversation_id_lands_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tentacle resolves the pre-ensured conversation by id — it never learns
    # why the conversation exists, only where to run.
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    parent = await conversations.ensure(_THREAD, agent_tentacle_id="inkling")
    child = await conversations.ensure(
        _THREAD,
        agent_tentacle_id="claude",
        subagent_id="repo-audit",
        parent_conversation_id=parent.id,
    )
    tentacle = _tentacle(conversations)

    result = await tentacle.run(
        "audit the repo",
        conversation_address=KEY,
        thread_id=_THREAD,
        run_name="commission",
        conversation_id=child.id,
    )

    assert result.output == "done"
    assert child.external_id == "sess-xyz"  # the hand's own resumable session
    assert child.runs  # the turn recorded into the child conversation


async def test_instructions_land_in_the_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run-level instructions (an accomplice's framing included) append to the
    # Claude Code system-prompt preset; the user prompt stays untouched.
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    await tentacle.run(
        "audit the repo",
        conversation_address=KEY,
        thread_id=_THREAD,
        instructions="You are an accomplice.",
    )

    options = cast(ClaudeAgentOptions, FakeClaudeClient.last_options)
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "You are an accomplice.",
    }
    assert FakeClaudeClient.last_prompt == "audit the repo"


async def test_a_non_interactive_run_declines_approvals_and_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hand has no user: approvals and questions die instantly instead of
    # becoming cards — the same policy the native runtimes apply to their own
    # subagents.
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    tentacle = _tentacle(conversations)

    await tentacle.run(
        "work", conversation_address=KEY, thread_id=_THREAD, interactive=False
    )

    options = cast(ClaudeAgentOptions, FakeClaudeClient.last_options)
    assert options.can_use_tool is not None
    decision = await options.can_use_tool(
        "Bash", {}, cast(ToolPermissionContext, SimpleNamespace(tool_use_id="t1"))
    )
    assert isinstance(decision, PermissionResultDeny)
    assert "no user" in decision.message
    assert options.hooks is not None
    ask_hooks = options.hooks["PreToolUse"][0].hooks
    answer = await ask_hooks[0](
        cast(HookInput, {"tool_input": {}}), "call-1", cast(HookContext, None)
    )
    assert answer == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "This run has no user to ask. Proceed "
            "on your best judgment and state the assumption in your report.",
        }
    }


async def test_run_resumes_prior_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    conversations = FakeConversationManager()
    conversations.store[(_THREAD, "claude", "")] = FakeConversation(
        external_id="prev-sess"
    )
    tentacle = _tentacle(conversations)

    result = await tentacle.run("again", conversation_address=KEY, thread_id=_THREAD)

    assert result.output == "done"
    options = FakeClaudeClient.last_options
    assert getattr(options, "resume", None) == "prev-sess"


class StructuredClaudeClient(FakeClaudeClient):
    """A run that returns a JSON structured result (an `output_format` run),
    e.g. Claude acting as a dispatch agent emitting a SummonDecision."""

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
            structured_output=_SUMMON_OUTPUT,
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
        thread_id=_THREAD,
        output_type=SummonDecision,
    )

    assert isinstance(result.output, SummonDecision)
    assert result.output.action == "summon"
    assert result.output.agent_id == "inkling"
    assert getattr(StructuredClaudeClient.last_options, "output_format", None) == {
        "type": "json_schema",
        "schema": SummonDecisionAdapter.json_schema(),
    }


async def test_run_uses_literal_output_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", LiteralClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    result = await tentacle.run(
        "triage this",
        conversation_address=KEY,
        thread_id=_THREAD,
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
        thread_id=_THREAD,
        output_type=SummonDecision | Literal["ignored"],
    )

    assert isinstance(result.output, SummonDecision)
    assert getattr(StructuredClaudeClient.last_options, "output_format", None) == {
        "type": "json_schema",
        "schema": TypeAdapter(SummonDecision | Literal["ignored"]).json_schema(),
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
            thread_id=_THREAD,
            output_type=[SummonDecision, DeferredToolRequests],
        )


async def test_run_honors_per_run_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    tentacle = _tentacle(
        FakeConversationManager(),
        config=ClaudeCodeConfig(models={"opus"}),
    )

    await tentacle.run("hi", conversation_address=KEY, thread_id=_THREAD, model="opus")

    assert getattr(FakeClaudeClient.last_options, "model", None) == "opus"


async def test_local_transport_passes_no_custom_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    tentacle = _tentacle(FakeConversationManager())  # default: no ssh block → local

    await tentacle.run("hi", conversation_address=KEY, thread_id=_THREAD)

    # A local run lets the SDK build its own subprocess transport.
    assert FakeClaudeClient.last_transport is None


async def test_run_tags_sdk_session_as_cli_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    await tentacle.run("hi", conversation_address=KEY, thread_id=_THREAD)

    assert getattr(FakeClaudeClient.last_options, "env", None) == {
        "CLAUDE_CODE_ENTRYPOINT": "cli"
    }


def test_configured_model_names_are_exposed() -> None:
    tentacle = _tentacle(
        FakeConversationManager(),
        config=ClaudeCodeConfig(models={"opus", "opusplan", "fable", "opus[1m]"}),
    )

    assert tentacle.models == {
        "opus": "opus",
        "opusplan": "opusplan",
        "fable": "fable",
        "opus[1m]": "opus[1m]",
    }


def test_build_structured_result_validates_into_model() -> None:
    accumulator = ClaudeRunAccumulator()
    accumulator.structured_output = _SUMMON_OUTPUT

    result = accumulator.build_structured_result(
        SummonDecisionAdapter, run_id="r1", conversation_id="c1"
    )

    assert isinstance(result.output, SummonDecision)
    assert result.output.action == "summon"


class GatedClaudeClient(FakeClaudeClient):
    """A client whose stream blocks mid-run until released or interrupted, so two
    runs on the same conversation can overlap deterministically (Phase 6)."""

    instances: ClassVar[list[GatedClaudeClient]] = []

    def __init__(self, options: object = None, transport: object = None) -> None:
        super().__init__(options, transport)
        self.interrupted = False
        self.released = asyncio.Event()
        GatedClaudeClient.instances.append(self)

    async def interrupt(self) -> None:
        self.interrupted = True
        self.released.set()

    async def receive_response(self) -> AsyncIterator[Message]:
        yield AssistantMessage(content=[TextBlock(text="working")], model="m")
        await self.released.wait()
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="done",
        )


async def _drive(tentacle: ClaudeCodeTentacle, prompt: str) -> None:
    async with tentacle.run_stream_events(
        prompt, conversation_address=KEY, thread_id=_THREAD
    ) as stream:
        async for _event in stream:
            pass


async def _spin_until(predicate: Callable[[], object]) -> None:
    for _ in range(100000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def test_new_run_interrupts_the_prior_live_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GatedClaudeClient.instances = []
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", GatedClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    # The live map keys on the conversation, not the thread — a thread also
    # holds subagent conversations whose runs must not interrupt this one.
    conversation = await tentacle.octomate.conversations.ensure(
        _THREAD, agent_tentacle_id="claude"
    )
    first = asyncio.ensure_future(_drive(tentacle, "first"))
    await _spin_until(lambda: tentacle.live_clients.get(conversation.id) is not None)
    client_a = GatedClaudeClient.instances[0]

    # A second turn on the same conversation supersedes the first: its client is
    # interrupted so the parked run unblocks and ends.
    second = asyncio.ensure_future(_drive(tentacle, "second"))
    await _spin_until(lambda: client_a.interrupted)

    # Release everything so both runs reach their terminal result.
    for instance in GatedClaudeClient.instances:
        instance.released.set()
    await asyncio.gather(first, second)

    assert client_a.interrupted
    # B superseded A: the live entry for the conversation is now B's client.
    assert tentacle.live_clients.get(conversation.id) is GatedClaudeClient.instances[1]


async def test_shutdown_interrupts_live_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GatedClaudeClient.instances = []
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", GatedClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    conversation = await tentacle.octomate.conversations.ensure(
        _THREAD, agent_tentacle_id="claude"
    )
    run = asyncio.ensure_future(_drive(tentacle, "hi"))
    await _spin_until(lambda: tentacle.live_clients.get(conversation.id) is not None)
    client = GatedClaudeClient.instances[0]

    await tentacle.__aexit__()

    assert client.interrupted
    assert len(tentacle.live_clients) == 0
    await run


async def test_completed_run_releases_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", FakeClaudeClient)
    tentacle = _tentacle(FakeConversationManager())

    await tentacle.run("hi", conversation_address=KEY, thread_id=_THREAD)

    # Weak-value map: a finished run's client is unreferenced, so its entry drops
    # on its own — no manual deregistration.
    gc.collect()
    assert len(tentacle.live_clients) == 0


def test_claims_come_from_config_and_default_to_none() -> None:
    """Claims are config-owned outright: a config claim is the tentacle's claim,
    and a bare config claims nothing — an unclaimed model cannot be summoned."""
    claim = Claim(ability="acme monorepo work", efforts=("high",))
    tentacle = _tentacle(
        FakeConversationManager(),
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS), claims={"haiku": claim}),
    )

    assert tentacle.claims == {"haiku": claim}
    assert ClaudeCodeConfig(models=set(CLAUDE_MODELS)).claims == {}
