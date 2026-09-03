from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from octomate_cli.stream import SESSION_FILE
from pydantic_ai.messages import ModelRequest, ModelResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import DEEPSEEK_NATIVE_ID, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.agents.deepseek.hooks import DeepseekHookInput
from octomate.tentacles.agents.deepseek.ingest import DeepseekHookIngest
from octomate.tentacles.agents.deepseek.tailer import DeepseekEventTailer, TailState
from octomate.types.json import JsonObject, JsonValue

SENDER = UserProfile(channel_user_id="lu", name="lu")

SESSION_ID = "session-native-0001"
BASE_TIME_MS = 1_786_899_000_000

# The log path in the *client's* namespace: a label the server records, never opens.
LOG_LABEL = Path(
    "/laptop/.dsh/sessions/--work-repo--/session-native-0001/session.jsonl"
)


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def wired(octomate: Octomate) -> tuple[DeepseekHookIngest, DeepseekEventTailer]:
    tailer = DeepseekEventTailer(
        octomate.conversations, octomate.thread_manager, octomate.projects
    )
    return DeepseekHookIngest(octomate, tailer), tailer


def ev(seq: int, kind: str, data: JsonValue) -> JsonObject:
    return {"type": kind, "seq": seq, "time": BASE_TIME_MS + seq * 1000, "data": data}


def user_message(
    seq: int, text: str, *, kind: str = "user", rpc_id: str | None = None
) -> JsonObject:
    source: JsonObject = {"kind": kind}
    if rpc_id is not None:
        source["rpcId"] = rpc_id
    return ev(
        seq,
        "user/message",
        {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "source": source,
            "id": f"user-{seq}",
        },
    )


def turn_events(
    turn: int,
    base_seq: int,
    prompt: str,
    answer: str,
    *,
    reason: str = "completed",
    rpc_id: str | None = None,
) -> list[JsonObject]:
    # The log's real per-turn order: the turn opens first, then dsh splices the
    # inbox into the step — the prompt lands *inside* the turn.
    return [
        ev(base_seq, "turn/start", {"turn": turn}),
        user_message(base_seq + 1, prompt, rpc_id=rpc_id),
        ev(
            base_seq + 2,
            "assistant/message",
            {
                "turn": turn,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": answer}],
                    "source": {
                        "kind": "model",
                        "provider": "deepseek-official",
                        "model": "deepseek-v4-flash",
                    },
                    "id": f"assistant-{turn}",
                },
                "usage": {"inputTokens": 10, "outputTokens": 5},
            },
        ),
        ev(base_seq + 3, "turn/end", {"turn": turn, "reason": {"kind": reason}}),
    ]


async def feed_events(
    tailer: DeepseekEventTailer, state: TailState, events: list[JsonObject]
) -> None:
    """Feed events as the stream client frames them — one history entry per
    line, seqs standing in for byte offsets."""
    for event in events:
        seq = event["seq"]
        assert isinstance(seq, int)
        await tailer.feed_remote(
            state, None, json.dumps({"event": event}), seq, seq + 1
        )


async def stream_events(
    tailer: DeepseekEventTailer,
    events: list[JsonObject],
    *,
    cwd: str = "/work/repo",
) -> dict[str, int]:
    """One connection's life as the route drives it: attach, feed, detach —
    returning the welcome offsets the attach answered."""
    state, offsets = await tailer.attach_remote(SESSION_ID, LOG_LABEL, cwd, SENDER)
    await feed_events(tailer, state, events)
    tailer.detach_remote(state)
    return offsets


async def native_conversation(octomate: Octomate):
    thread = await octomate.thread_manager.ensure(
        ThreadKey(DEEPSEEK_NATIVE_ID, "thread", SESSION_ID)
    )
    return await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=DEEPSEEK_NATIVE_ID
    )


async def test_streamed_events_assemble_the_turn_and_its_ledger() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)

    offsets = await stream_events(tailer, turn_events(1, 0, "inspect it", "done"))

    assert offsets == {SESSION_FILE: 0}
    conversation = await native_conversation(octomate)
    [run] = conversation.runs
    assert isinstance(run, ExternalAgentRun)
    assert run.id == f"{SESSION_ID}:1"
    assert run.start_offset == 0
    assert run.end_offset == 3
    assert run.source == "local"
    assert [message.message_text for message in run.messages] == ["inspect it", "done"]

    thread = await octomate.thread_manager.ensure(
        ThreadKey(DEEPSEEK_NATIVE_ID, "thread", SESSION_ID)
    )
    directions = [
        (message.direction, message.message_text) for message in thread.messages
    ]
    assert directions == [("inbound", "inspect it"), ("outbound", "done")]


async def test_injected_user_messages_stay_out_of_the_prompt_row() -> None:
    """dsh logs `agent-instructions` and `plugin` context as user-role
    messages inside the turn; the ledger's prompt is the human's words only —
    the opening prompt plus anything steered in later."""
    octomate = Octomate()
    _, tailer = wired(octomate)

    await stream_events(
        tailer,
        [
            ev(0, "turn/start", {"turn": 1}),
            user_message(1, "hi", rpc_id="rpc-1"),
            user_message(
                2,
                "<system-reminder>workspace rules</system-reminder>",
                kind="agent-instructions",
            ),
            user_message(3, "Current runtime context.", kind="plugin"),
            user_message(4, "also check the tests", rpc_id="rpc-2"),
            ev(
                5,
                "assistant/message",
                {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "source": {"kind": "model", "provider": "p", "model": "m"},
                        "id": "assistant-1",
                    },
                },
            ),
            ev(6, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ],
    )

    conversation = await native_conversation(octomate)
    [run] = conversation.runs
    request = next(m for m in run.messages if isinstance(m, ModelRequest))
    assert request.parts[0].content == "hi\n\nalso check the tests"
    assert run.source == "gateway"
    thread = await octomate.thread_manager.ensure(
        ThreadKey(DEEPSEEK_NATIVE_ID, "thread", SESSION_ID)
    )
    inbound = [m.message_text for m in thread.messages if m.direction == "inbound"]
    assert inbound == ["hi\n\nalso check the tests"]


async def test_a_reconnect_resumes_past_the_committed_floor() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)
    await stream_events(tailer, turn_events(1, 0, "first", "one"))

    # The next connect is told to resume at the seq after the committed turn;
    # re-feeding the same turn regardless commits nothing new.
    state, offsets = await tailer.attach_remote(
        SESSION_ID, LOG_LABEL, "/work/repo", SENDER
    )
    assert offsets == {SESSION_FILE: 4}
    await feed_events(
        tailer,
        state,
        [*turn_events(1, 0, "first", "one"), *turn_events(2, 4, "second", "two")],
    )
    tailer.detach_remote(state)

    conversation = await native_conversation(octomate)
    assert sorted(run.id for run in conversation.runs) == [
        f"{SESSION_ID}:1",
        f"{SESSION_ID}:2",
    ]


async def test_an_open_turn_never_commits_at_a_connection_boundary() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)

    # turn/end never arrives: the tail dropped mid-turn.
    await stream_events(tailer, turn_events(1, 0, "hold", "partial")[:-1])
    conversation = await native_conversation(octomate)
    assert conversation.runs == []

    # The next connect re-streams the turn whole and it lands.
    await stream_events(tailer, turn_events(1, 0, "hold", "partial"))
    conversation = await native_conversation(octomate)
    [run] = conversation.runs
    assert run.id == f"{SESSION_ID}:1"


async def test_runs_are_dated_by_the_log_not_the_replay() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)

    await stream_events(tailer, turn_events(1, 0, "when", "then"))

    conversation = await native_conversation(octomate)
    [run] = conversation.runs
    request = next(m for m in run.messages if isinstance(m, ModelRequest))
    response = next(m for m in run.messages if isinstance(m, ModelResponse))
    assert request.timestamp == datetime.fromtimestamp(
        (BASE_TIME_MS + 1000) / 1000, tz=UTC
    )
    assert response.timestamp == datetime.fromtimestamp(
        (BASE_TIME_MS + 2000) / 1000, tz=UTC
    )


async def test_a_gateway_prompt_marks_the_run_and_a_local_one_does_not() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)

    await stream_events(
        tailer,
        [
            *turn_events(1, 0, "typed locally", "one"),
            *turn_events(2, 4, "sent via api", "two", rpc_id="rpc-1"),
        ],
    )

    conversation = await native_conversation(octomate)
    sources = {run.id: run.source for run in conversation.runs}
    assert sources == {
        f"{SESSION_ID}:1": "local",
        f"{SESSION_ID}:2": "gateway",
    }


async def test_the_observed_permission_preset_lands_on_the_conversation() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)

    await stream_events(
        tailer,
        [
            ev(0, "permission/preset", {"preset": "danger-full-access"}),
            *turn_events(1, 1, "go", "gone"),
        ],
    )
    conversation = await native_conversation(octomate)
    assert conversation.permission_mode == "danger-full-access"


async def test_an_unmodeled_preset_is_observed_but_not_stored() -> None:
    octomate = Octomate()
    _, tailer = wired(octomate)

    await stream_events(
        tailer,
        [
            ev(0, "permission/preset", {"preset": "read-only-audit"}),
            *turn_events(1, 1, "go", "gone"),
        ],
    )
    conversation = await native_conversation(octomate)
    assert conversation.permission_mode is None


async def test_driven_session_hooks_are_ignored() -> None:
    octomate = Octomate()
    ingest, _ = wired(octomate)

    with ingest.driving(SESSION_ID):
        await ingest.handle(
            DeepseekHookInput(hook_event_name="Stop", session_id=SESSION_ID)
        )
        await ingest.handle(
            DeepseekHookInput(
                hook_event_name="UserPromptSubmit",
                session_id=SESSION_ID,
                prompt="driven",
            )
        )
    assert ingest.tasks == set()
    assert await octomate.thread_manager.list_threads() == []


async def test_a_prompt_hook_creates_the_session_skeleton() -> None:
    octomate = Octomate()
    ingest, _ = wired(octomate)

    await ingest.handle(
        DeepseekHookInput(
            hook_event_name="UserPromptSubmit",
            session_id=SESSION_ID,
            cwd="/work/repo",
            prompt="a new prompt",
        )
    )

    thread = await octomate.thread_manager.ensure(
        ThreadKey(DEEPSEEK_NATIVE_ID, "thread", SESSION_ID)
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=DEEPSEEK_NATIVE_ID
    )
    assert conversation.runs == []


async def test_a_stop_waits_for_the_stopped_turn_then_asks_the_drain() -> None:
    """dsh fires `Stop` before the turn's `turn/end` is even emitted, so the
    settle (running detached from the hook) must wait for the streamed close
    to commit before relaying the finalize."""
    octomate = Octomate()
    ingest, tailer = wired(octomate)
    state, _ = await tailer.attach_remote(SESSION_ID, LOG_LABEL, "/work/repo", SENDER)
    events = turn_events(1, 0, "stopping", "stopped")
    await feed_events(tailer, state, events[:-1])

    await ingest.handle(
        DeepseekHookInput(hook_event_name="Stop", session_id=SESSION_ID)
    )
    [settle] = ingest.tasks
    await asyncio.sleep(0)
    assert not state.stop_event.is_set()

    # The seam releases, dsh flushes the close, the stream ships it: the
    # commit pulses the settle and the finalize goes out.
    await feed_events(tailer, state, events[-1:])
    await asyncio.wait_for(settle, timeout=2)
    assert state.stop_event.is_set()

    conversation = await native_conversation(octomate)
    [run] = conversation.runs
    assert run.id == f"{SESSION_ID}:1"
    tailer.detach_remote(state)


async def test_a_stop_with_no_attached_stream_settles_nothing() -> None:
    octomate = Octomate()
    ingest, _ = wired(octomate)

    await ingest.handle(
        DeepseekHookInput(hook_event_name="Stop", session_id=SESSION_ID)
    )
    await asyncio.gather(*ingest.tasks)
