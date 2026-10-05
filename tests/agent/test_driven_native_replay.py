"""A native resume retains new turns without copying already driven work."""

import json
from pathlib import Path
from typing import Literal

import pytest
from octomate_protocol.stream import SESSION_FILE
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.schemas.thread import ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude.hooks import ClaudeHookInput
from octomate.tentacles.claude.ingest import ClaudeHookIngest
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.codex.hooks import CodexHookInput
from octomate.tentacles.codex.ingest import CodexHookIngest
from octomate.tentacles.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.deepseek.tailer import DeepseekEventTailer
from tests.agent.test_claude_tailer import assistant_record, prompt_record
from tests.agent.test_deepseek_native_ingest import turn_events
from tests.support.managers import a_thread

SESSION_ID = "sess-tail"
SENDER = UserProfile(channel_user_id="lu", name="lu")
Runtime = Literal["claude", "codex", "deepseek"]


@pytest.mark.parametrize("runtime", ["claude", "codex", "deepseek"])
@pytest.mark.parametrize("restart", [False, True])
async def test_replayed_driven_turn_is_skipped_and_new_native_turn_is_kept(
    in_memory_engine: AsyncEngine, runtime: Runtime, restart: bool
) -> None:
    octomate = Octomate()
    native_id = f"{runtime}-native"
    old_turn = f"{SESSION_ID}:1" if runtime == "deepseek" else "p1"
    new_turn = f"{SESSION_ID}:2" if runtime == "deepseek" else "p2"
    driven = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id=f"custom-{runtime}"
    )
    await octomate.conversations.record_agent_run(
        driven,
        run_id="driven-run",
        messages=[
            ModelRequest(parts=[UserPromptPart(content="same prompt")]),
            ModelResponse(parts=[TextPart(content="same answer")]),
        ],
        external_id=SESSION_ID,
        native_id=native_id,
        native_turn_id=old_turn,
    )
    if restart:
        octomate = Octomate()

    if runtime == "claude":
        tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
        ingest = ClaudeHookIngest(octomate, tailer)
        await ingest.handle(
            ClaudeHookInput(
                hook_event_name="UserPromptSubmit",
                session_id=SESSION_ID,
                prompt_id=old_turn,
                prompt="same prompt",
            ),
            SENDER,
        )
    elif runtime == "codex":
        codex_tailer = CodexTranscriptTailer(
            octomate.conversations, octomate.thread_manager
        )
        codex_ingest = CodexHookIngest(octomate, codex_tailer)
        await codex_ingest.handle(
            CodexHookInput(
                hook_event_name="UserPromptSubmit",
                session_id=SESSION_ID,
                turn_id=old_turn,
                prompt="same prompt",
            ),
            SENDER,
        )

    async with async_session() as session:
        assert await session.count(ThreadMessage) == 0

    for turn in (1, 2):
        if runtime == "claude":
            records = [
                record
                for replay_turn in range(1, turn + 1)
                for record in (
                    prompt_record(f"p{replay_turn}", "same prompt", replay_turn * 2),
                    assistant_record(
                        f"a{replay_turn}",
                        replay_turn * 2 + 1,
                        [{"type": "text", "text": "same answer"}],
                    ),
                )
            ]
            tailer = ClaudeTranscriptTailer(
                octomate.conversations, octomate.thread_manager
            )
            state, offsets = await tailer.attach_remote(
                SESSION_ID, Path("/client/session.jsonl"), SENDER
            )
            assert offsets == {SESSION_FILE: 0}
            offset = 0
            for record in records:
                line = json.dumps(record)
                end = offset + len(line.encode()) + 1
                await tailer.feed_remote(state, None, line, offset, end)
                offset = end
            await tailer.finish_remote(state)
        elif runtime == "codex":
            codex_tailer = CodexTranscriptTailer(
                octomate.conversations, octomate.thread_manager
            )
            codex_state, offsets = await codex_tailer.attach_remote(
                SESSION_ID, Path("/client/rollout.jsonl"), SENDER
            )
            assert offsets == {SESSION_FILE: 0}
            offset = 0
            payloads: list[dict[str, str]] = [
                payload
                for replay_turn in range(1, turn + 1)
                for payload in (
                    {"type": "task_started", "turn_id": f"p{replay_turn}"},
                    {"type": "user_message", "message": "same prompt"},
                    {
                        "type": "task_complete",
                        "turn_id": f"p{replay_turn}",
                        "last_agent_message": "same answer",
                    },
                )
            ]
            for payload in payloads:
                line = json.dumps(
                    {
                        "timestamp": "2026-09-24T12:00:00Z",
                        "type": "event_msg",
                        "payload": payload,
                    }
                )
                end = offset + len(line.encode()) + 1
                await codex_tailer.feed_remote(codex_state, None, line, offset, end)
                offset = end
            codex_tailer.detach_remote(codex_state)
        else:
            deepseek_tailer = DeepseekEventTailer(
                octomate.conversations, octomate.thread_manager
            )
            deepseek_state, offsets = await deepseek_tailer.attach_remote(
                SESSION_ID, Path("/client/session.jsonl"), "", SENDER
            )
            assert offsets == {SESSION_FILE: 0}
            events = [
                event
                for replay_turn in range(1, turn + 1)
                for event in turn_events(
                    replay_turn, (replay_turn - 1) * 4, "same prompt", "same answer"
                )
            ]
            for event in events:
                await deepseek_tailer.feed_remote(
                    deepseek_state, None, json.dumps({"event": event}), 0, 0
                )
            deepseek_tailer.detach_remote(deepseek_state)

        async with async_session() as session:
            runs = await session.list(AgentRun, limit=None, order_bys=[])
            assert {run.id for run in runs} == (
                {"driven-run"} if turn == 1 else {"driven-run", new_turn}
            )
            stored = await session.get(AgentRun, "driven-run")
            assert stored is not None
            assert stored.conversation_id == driven.id
            assert not isinstance(stored, ExternalAgentRun)
            assert len(stored.messages) == 2
            external_runs = [run for run in runs if isinstance(run, ExternalAgentRun)]
            assert len(external_runs) == turn - 1
            assert all(run.end_offset is not None for run in external_runs)
            assert await session.count(ThreadMessage) == (turn - 1) * 2
        octomate = Octomate()


@pytest.mark.parametrize(
    ("native_id", "session_id", "turn_id"),
    [
        ("codex-native", SESSION_ID, "p1"),
        ("claude-native", "other-session", "p1"),
        ("claude-native", SESSION_ID, "other-turn"),
    ],
)
async def test_driven_identity_does_not_match_another_runtime_session_or_turn(
    in_memory_engine: AsyncEngine, native_id: str, session_id: str, turn_id: str
) -> None:
    octomate = Octomate()
    driven = await octomate.conversations.ensure(
        await a_thread(), agent_tentacle_id="custom-claude"
    )
    await octomate.conversations.record_agent_run(
        driven,
        run_id="driven-run",
        messages=[ModelRequest(parts=[UserPromptPart(content="same prompt")])],
        native_id="claude-native",
        external_id=SESSION_ID,
        native_turn_id="p1",
    )
    assert (
        await octomate.conversations.driven_run(native_id, session_id, turn_id) is None
    )
