"""OCTO-30 — every run records the directory it ran in.

`AgentRun.cwd` is written by all three writers: the hooks' live sketch, the tailer's
turn rebuilt from the transcript, and Octomate-driven dispatch. The data was already
arriving on every hook event and every transcript line and was being dropped.

The column is on the polymorphic base, so a native turn and a driven run answer "where
did this run" the same way. A source that reported no directory records None — never a
guess, and never Claude's transcript directory slug, whose `-` is ambiguous between a
separator and a literal dash.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig, CodexConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import Thread, ThreadKey
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.claude import base as claude_base
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.codex import base as codex_base
from octomate.tentacles.agents.codex.hooks import CodexHookInput
from octomate.tentacles.agents.codex.ingest import CODEX_NATIVE_ID, CodexHookIngest
from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.agents.locks import SessionLocks
from octomate.types.json import JsonObject
from tests.agent.test_codex_native_ingest import stream_rollout
from tests.agent.test_codex_tentacle import FakeCodex, reset_fake_codex, text_script
from tests.support.agents import CLAUDE_MODELS, CODEX_MODELS, RecordingClaudeClient
from tests.support.managers import a_project, a_registry

CLAUDE_SESSION = "sess-cwd"
CODEX_SESSION = "codex-cwd"
CODEX_TURN = "codex-turn-cwd"
REPO = "/repo"
ELSEWHERE = "/repo/docs"
ADDRESS = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)
HOOK_SECRET = SecretStr("test-hook-secret")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture
def _fake_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", RecordingClaudeClient)
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))


async def runs_of(
    octomate: Octomate, tentacle_id: str, session_id: str
) -> list[AgentRun]:
    thread = await octomate.thread_manager.ensure(
        ThreadKey(tentacle_id, "thread", session_id)
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=tentacle_id
    )
    return list(conversation.runs)


# --- native Claude ----------------------------------------------------------------


def claude_prompt(prompt_id: str, text: str, second: int, cwd: str) -> JsonObject:
    return {
        "type": "user",
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd,
        "sessionId": CLAUDE_SESSION,
        "version": "2.1.0",
        "gitBranch": "main",
        "parentUuid": None,
        "entrypoint": "cli",
        "promptSource": "cli",
        "promptId": prompt_id,
        "uuid": f"u-{prompt_id}",
        "timestamp": f"2026-08-09T10:00:{second:02d}.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def claude_answer(uid: str, second: int, text: str, cwd: str) -> JsonObject:
    return {
        "type": "assistant",
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd,
        "sessionId": CLAUDE_SESSION,
        "version": "2.1.0",
        "gitBranch": "main",
        "parentUuid": None,
        "entrypoint": "cli",
        "uuid": uid,
        "timestamp": f"2026-08-09T10:00:{second:02d}.000Z",
        "message": {
            "id": f"msg-{uid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-8",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "content": [{"type": "text", "text": text}],
        },
    }


def write_transcript(path: Path, records: Sequence[JsonObject]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


async def claude_hook(octomate: Octomate, cwd: str) -> None:
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    ingest = ClaudeHookIngest(octomate, tailer)
    await ingest.handle(
        ClaudeHookInput.model_validate(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": CLAUDE_SESSION,
                "cwd": cwd,
                "prompt": "hi",
                "prompt_id": "p1",
            }
        )
    )


async def test_a_claude_hook_sketch_records_the_directory_the_hook_reported() -> None:
    octomate = Octomate()

    await claude_hook(octomate, REPO)

    [sketch] = await runs_of(octomate, CLAUDE_NATIVE_ID, CLAUDE_SESSION)
    assert sketch.cwd == Path(REPO)


async def test_a_claude_hook_with_no_cwd_records_none() -> None:
    # `Path("")` is the process's own directory, so anything but None here would be a
    # guess dressed as a fact.
    octomate = Octomate()

    await claude_hook(octomate, "")

    [sketch] = await runs_of(octomate, CLAUDE_NATIVE_ID, CLAUDE_SESSION)
    assert sketch.cwd is None


async def stream_transcript(tailer: ClaudeTranscriptTailer, transcript: Path) -> None:
    """Stream one transcript file the way production reaches it: attach and feed its
    framed lines — the server never opens the file itself."""
    state, _ = await tailer.attach_remote(CLAUDE_SESSION, transcript)
    offset = 0
    for raw in transcript.read_bytes().split(b"\n")[:-1]:
        end = offset + len(raw) + 1
        await tailer.feed_remote(state, None, raw.decode(), offset, end)
        offset = end
    await tailer.finish_remote(state)


async def test_each_claude_turn_records_its_own_prompt_directory(
    tmp_path: Path,
) -> None:
    # A transcript's cwd is per line, so a session's directory is not one value: the
    # human can `cd` between turns and the next prompt lands somewhere else.
    transcript = tmp_path / f"{CLAUDE_SESSION}.jsonl"
    write_transcript(
        transcript,
        [
            claude_prompt("p1", "here", 1, REPO),
            claude_answer("a1", 2, "ok", REPO),
            claude_prompt("p2", "and here", 3, ELSEWHERE),
            claude_answer("a2", 4, "ok", ELSEWHERE),
        ],
    )
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_transcript(tailer, transcript)

    runs = await runs_of(octomate, CLAUDE_NATIVE_ID, CLAUDE_SESSION)
    assert {run.id: run.cwd for run in runs} == {
        "p1": Path(REPO),
        "p2": Path(ELSEWHERE),
    }


async def test_a_claude_turn_keeps_the_directory_it_was_asked_in(
    tmp_path: Path,
) -> None:
    # Claude's shell is persistent, so a `cd` mid-turn moves the cwd of every line
    # after it. The turn ran where the human asked, not wherever a tool wandered to.
    transcript = tmp_path / f"{CLAUDE_SESSION}.jsonl"
    write_transcript(
        transcript,
        [
            claude_prompt("p1", "look around", 1, REPO),
            claude_answer("a1", 2, "ok", "/repo/.venv/lib/python3.13"),
        ],
    )
    octomate = Octomate()
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)

    await stream_transcript(tailer, transcript)

    [run] = await runs_of(octomate, CLAUDE_NATIVE_ID, CLAUDE_SESSION)
    assert run.cwd == Path(REPO)


async def test_the_rebuilt_claude_turn_supersedes_the_sketch_directory(
    tmp_path: Path,
) -> None:
    # The tailer replaces the hooks' sketch wholesale, so the transcript's answer is
    # the one that survives — including for a hook that carried no directory at all.
    transcript = tmp_path / f"{CLAUDE_SESSION}.jsonl"
    transcript.write_text("")
    octomate = Octomate()
    locks = SessionLocks()
    tailer = ClaudeTranscriptTailer(
        octomate.conversations, octomate.thread_manager, locks
    )
    ingest = ClaudeHookIngest(octomate, tailer, locks)

    await ingest.handle(
        ClaudeHookInput.model_validate(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": CLAUDE_SESSION,
                "cwd": "",
                "prompt": "here",
                "prompt_id": "p1",
                "transcript_path": str(transcript),
            }
        )
    )
    [sketch] = await runs_of(octomate, CLAUDE_NATIVE_ID, CLAUDE_SESSION)
    assert sketch.cwd is None

    write_transcript(
        transcript,
        [claude_prompt("p1", "here", 1, REPO), claude_answer("a1", 2, "ok", REPO)],
    )
    await stream_transcript(tailer, transcript)

    [run] = await runs_of(octomate, CLAUDE_NATIVE_ID, CLAUDE_SESSION)
    assert run.cwd == Path(REPO)


# --- native Codex -----------------------------------------------------------------


def codex_rollout(cwd: str) -> list[JsonObject]:
    return [
        {
            "timestamp": "2026-08-09T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": CODEX_SESSION,
                "cwd": cwd,
                "originator": "codex_cli",
            },
        },
        {
            "timestamp": "2026-08-09T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": CODEX_TURN},
        },
        {
            "timestamp": "2026-08-09T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect it"},
        },
        {
            "timestamp": "2026-08-09T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "message-1",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        {
            "timestamp": "2026-08-09T10:00:04Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": CODEX_TURN},
        },
    ]


async def test_a_codex_hook_sketch_records_the_directory_the_hook_reported() -> None:
    octomate = Octomate()
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.thread_manager)
    ingest = CodexHookIngest(octomate, tailer)

    await ingest.handle(
        CodexHookInput.model_validate(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": CODEX_SESSION,
                "cwd": REPO,
                "prompt": "hi",
                "turn_id": CODEX_TURN,
            }
        )
    )

    [sketch] = await runs_of(octomate, CODEX_NATIVE_ID, CODEX_SESSION)
    assert sketch.cwd == Path(REPO)


async def codex_rollout_run(octomate: Octomate, rollout: Path) -> AgentRun:
    """Stream one rollout the way production reaches it: a tail attaches and feeds
    the file's framed lines, and the turn commits off its own `task_complete`."""
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.thread_manager)
    await stream_rollout(tailer, CODEX_SESSION, rollout)
    [run] = await runs_of(octomate, CODEX_NATIVE_ID, CODEX_SESSION)
    return run


async def test_a_codex_turn_from_the_rollout_records_the_session_directory(
    tmp_path: Path,
) -> None:
    # Codex reports its directory once, in the rollout's session metadata, and repeats
    # it per turn in `turn_context` without ever disagreeing with itself.
    rollout = tmp_path / "rollout.jsonl"
    write_transcript(rollout, codex_rollout(REPO))
    octomate = Octomate()

    assert (await codex_rollout_run(octomate, rollout)).cwd == Path(REPO)


async def test_a_codex_rollout_naming_no_directory_records_none(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    write_transcript(rollout, codex_rollout(""))
    octomate = Octomate()

    assert (await codex_rollout_run(octomate, rollout)).cwd is None


# --- Octomate-driven dispatch -----------------------------------------------------


async def a_thread(octomate: Octomate, project: str = "") -> Thread:
    return await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", "chat", "t1"),
        project=octomate.projects.get(project) if project else None,
    )


async def driven_runs(
    octomate: Octomate, thread: Thread, agent_id: str
) -> list[AgentRun]:
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=agent_id
    )
    return list(conversation.runs)


@pytest.mark.usefixtures("_fake_runtimes")
async def test_a_driven_claude_run_records_where_it_dispatched(
    tmp_path: Path,
) -> None:
    inky = tmp_path / "inky"
    inky.mkdir()
    octomate = Octomate(projects=await a_registry(a_project(inky)))
    thread = await a_thread(octomate, "inky")
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS), cwd="/configured"),
        hook_secret=HOOK_SECRET,
    )

    async with tentacle.run_stream_events(
        "do it", conversation_address=ADDRESS, thread_id=thread.id, run_name="react"
    ) as stream:
        async for _event in stream:
            pass

    options: ClaudeAgentOptions | None = RecordingClaudeClient.last_options
    assert options is not None
    workspace = octomate.workspaces.path(thread.id)
    assert options.cwd == str(workspace)
    [run] = await driven_runs(octomate, thread, "claude")
    # The same directory the run was launched in, so a driven run answers "where did
    # this run" exactly as a native one does — the thread's workspace since OCTO-48,
    # which is what makes the answer specific enough to go back to.
    assert run.cwd == workspace


@pytest.mark.usefixtures("_fake_runtimes")
async def test_a_driven_codex_run_records_where_it_dispatched() -> None:
    octomate = Octomate()
    thread = await a_thread(octomate)
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(
            models=set(CODEX_MODELS), permission_mode="deny_all", cwd="/configured"
        ),
        hook_secret=HOOK_SECRET,
    )

    async with tentacle:
        async with tentacle.run_stream_events(
            "do it", conversation_address=ADDRESS, thread_id=thread.id, run_name="react"
        ) as stream:
            async for _event in stream:
                pass

    # No project declared, so dispatch stays where it is configured — and that is
    # what the run records.
    [run] = await driven_runs(octomate, thread, "codex")
    assert run.cwd == Path("/configured")
