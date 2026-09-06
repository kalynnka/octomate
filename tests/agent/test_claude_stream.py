"""Transcript ingest over the stream endpoint — the only assembler: raw framed
lines fed per file. The server owns every cursor — a connect is answered with where
each file resumes, computed from the committed runs — and a connection that drops
without `eof` leaves its open turns for the next connect to re-stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.tentacles.claude import CLAUDE_HOOK_PATH, CLAUDE_STREAM_PATH
from octomate_protocol.stream import (
    SESSION_FILE,
    STREAM_PROTOCOL,
    StreamEof,
    StreamFinalize,
    StreamHello,
    StreamLine,
    StreamWelcome,
    server_message_adapter,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from octomate import Octomate
from octomate.config import ClaudeCodeConfig
from octomate.managers.user import UserManager
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer, TailState
from octomate.types.json import JsonObject
from tests.agent.test_claude_tailer import (
    AGENT_ID,
    SESSION_ID,
    SUB_TURN_ONE,
    TURN_ONE,
    TURN_TWO,
    agent_spawn_records,
    line_bytes,
    runs_of,
    subagent_runs_of,
)
from tests.support.agents import CLAUDE_MODELS
from tests.support.config import registered

SENDER = UserProfile(channel_user_id="lu", name="lu")

SECRET = SecretStr("the-hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}
# The transcript in the *client's* namespace: a label the server records, never opens.
CLIENT_PATH = Path("/laptop/.claude/projects/-repo") / f"{SESSION_ID}.jsonl"

Frame = tuple[str | None, int, int, str]


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def frames(
    records: list[JsonObject], *, agent_id: str | None = None, start: int = 0
) -> list[Frame]:
    """Frame records exactly as the client does: complete lines with the byte range
    each occupies in its own file, `end` past the framing newline."""
    framed: list[Frame] = []
    offset = start
    for record in records:
        raw = line_bytes(record)
        framed.append((agent_id, offset, offset + len(raw), raw[:-1].decode()))
        offset += len(raw)
    return framed


async def feed(
    tailer: ClaudeTranscriptTailer, state: TailState, framed: list[Frame]
) -> None:
    for agent_id, start, end, line in framed:
        await tailer.feed_remote(state, agent_id, line, start, end)


def remote_tailer() -> tuple[Octomate, ClaudeTranscriptTailer]:
    octomate = Octomate()
    return octomate, ClaudeTranscriptTailer(
        octomate.conversations, octomate.thread_manager
    )


async def test_remote_feed_assembles_the_same_runs_as_a_local_tail() -> None:
    octomate, tailer = remote_tailer()

    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets == {SESSION_FILE: 0}
    assert SESSION_ID in tailer.sessions  # so hooks route the drains here
    await feed(tailer, state, frames(TURN_ONE + TURN_TWO))
    await tailer.finish_remote(state)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]
    p1, p2 = runs
    # The client's offsets land as the runs' byte ranges: contiguous and gapless.
    assert p1.start_offset == 0
    assert p1.end_offset == p2.start_offset
    kinds = [type(message).__name__ for message in p1.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse"]
    assert SESSION_ID not in tailer.sessions  # registry slot reclaimed


async def test_a_reconnect_is_told_to_resume_from_the_committed_runs() -> None:
    octomate, tailer = remote_tailer()

    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames(TURN_ONE))
    await tailer.finish_remote(state)
    (p1,) = await runs_of(octomate)

    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets[SESSION_FILE] == p1.end_offset
    await feed(tailer, state, frames(TURN_TWO, start=p1.end_offset or 0))
    await tailer.finish_remote(state)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]
    assert runs[1].start_offset == p1.end_offset  # no gap, no overlap


async def test_a_drop_without_eof_leaves_open_turns_for_the_next_connect() -> None:
    """Mid-turn bytes are never provably complete when the socket dies, so nothing
    commits — committing a partial turn would advance the resume mark past bytes no
    run covers, stranding the rest of the turn where no recovery could reach it."""
    octomate, tailer = remote_tailer()

    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames(TURN_ONE))  # open turn: no closing prompt yet
    tailer.detach_remote(state)

    assert await runs_of(octomate) == []
    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets == {SESSION_FILE: 0}  # nothing committed: re-stream from the top
    await feed(tailer, state, frames(TURN_ONE + TURN_TWO))
    await tailer.finish_remote(state)
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_finalize_relays_through_stop_event_and_waits_for_the_drain() -> None:
    """`SessionEnd` reaches a remote session as its `stop_event` — the stream route
    sends the client `finalize` on it — and `finalize` returns once `finish_remote`
    has committed, not after its timeout."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames(TURN_ONE))

    finalizing = asyncio.create_task(tailer.finalize(SESSION_ID))
    await asyncio.wait_for(state.stop_event.wait(), 2)
    # The route's reaction, compressed: the client drains (nothing new) and eofs.
    await tailer.finish_remote(state)
    await asyncio.wait_for(finalizing, 2)  # well under REMOTE_DRAIN_TIMEOUT

    assert [run.id for run in await runs_of(octomate)] == ["p1"]


async def test_a_stop_drains_the_remote_tail_and_commits_the_stopped_turn() -> None:
    """`Stop` reaches a remote session as a per-turn drain: `stop_turn` sets the
    `stop_event` the route's relay watches, and the `eof` answering the relayed
    `finalize` commits the stopped turn — the next prompt no longer gates it."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames(TURN_ONE))  # open turn: no closing prompt yet

    await tailer.stop_turn(SESSION_ID, "p1")
    assert state.stop_event.is_set()  # what the route's finalize relay watches
    await tailer.finish_remote(state)  # the client's drain answered eof

    (p1,) = await runs_of(octomate)
    assert p1.id == "p1"
    assert p1.end_offset is not None
    assert SESSION_ID not in tailer.sessions  # the tail is done; slot reclaimed


async def test_a_prompt_queued_into_the_drain_stays_open_for_the_next_connect() -> None:
    """A queued message can land between `Stop` and the drain's `eof`, so its prompt
    line ships with the drain and opens the next turn mid-drain. Only the stopped
    turn may commit: a partial next turn committed here would hold its id against the
    complete version forever."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames(TURN_ONE))
    await tailer.stop_turn(SESSION_ID, "p1")

    total = sum(len(line_bytes(record)) for record in TURN_ONE)
    await feed(tailer, state, frames(TURN_TWO[:1], start=total))  # p2's prompt line
    await tailer.finish_remote(state)

    (p1,) = await runs_of(octomate)  # p1 closed on its own boundary; p2 not committed
    assert p1.id == "p1"

    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets[SESSION_FILE] == p1.end_offset  # p2's line re-streams whole
    tailer.detach_remote(state)


async def test_subagent_lines_stream_into_child_runs() -> None:
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    # The parent turn spawns the child (its tool-result names the agent id), then the
    # child's own file streams under its agent id.
    await feed(tailer, state, frames([TURN_ONE[0], *agent_spawn_records("p1", 2)]))
    await feed(tailer, state, frames(SUB_TURN_ONE, agent_id=AGENT_ID))
    await tailer.finish_remote(state)

    (child,) = await subagent_runs_of(octomate)
    assert child.id == f"{AGENT_ID}:p1"
    assert child.parent_run_id == "p1"
    assert child.parent_tool_call_id == f"toolu-agent-{AGENT_ID}"

    # A reconnect is told where the child's file resumes too.
    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets[AGENT_ID] == child.end_offset
    tailer.detach_remote(state)


async def test_a_new_attach_replaces_a_lingering_registration() -> None:
    """A client that died without a close leaves its registration behind; the next
    connect replaces it, and the dead route's detach must not evict the
    replacement."""
    _, tailer = remote_tailer()
    stale, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    fresh, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert tailer.sessions[SESSION_ID] is fresh
    tailer.detach_remote(stale)
    assert tailer.sessions[SESSION_ID] is fresh
    tailer.detach_remote(fresh)
    assert SESSION_ID not in tailer.sessions


def stream_client() -> tuple[TestClient, ClaudeCodeTentacle]:
    config = registered(SECRET.get_secret_value())
    octomate = Octomate(config=config, users=UserManager(config.users))
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS)),
    )

    # Entering the client runs the lifespan: the registered user gets their
    # registry row, the way the real app reconciles before serving.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await octomate.users.reconcile()
        yield

    app = FastAPI(lifespan=lifespan)
    for router in tentacle.routers():
        app.include_router(router)
    return TestClient(app), tentacle


def hello_json(session_id: str = SESSION_ID, protocol: int = STREAM_PROTOCOL) -> str:
    return StreamHello(
        protocol=protocol,
        session_id=session_id,
        transcript_path=str(CLIENT_PATH),
        cwd="/repo",
    ).model_dump_json()


def test_the_stream_authenticates_like_the_hook_routers() -> None:
    """Reachability is not a credential on the socket either: the router's
    `hook_guard` dependency runs at the handshake, so no bearer means the same 401
    the hook routes answer, before any socket opens."""
    client, _ = stream_client()
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(CLAUDE_STREAM_PATH):
            pass
    assert denial.value.status_code == 401


def test_lines_over_the_socket_commit_and_the_next_welcome_resumes() -> None:
    """End to end through the endpoint: hello/welcome, framed lines, eof — and the
    proof of commit is protocol-visible, because the next connect's welcome resumes
    past everything the first one shipped."""
    client, _ = stream_client()
    total = sum(len(line_bytes(record)) for record in TURN_ONE + TURN_TWO)

    # One entered client, so both connects share a portal loop — the in-memory
    # database's connection is loop-bound, and two portals would strand it.
    with client:
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 0}
            for agent_id, start, end, line in frames(TURN_ONE + TURN_TWO):
                websocket.send_text(
                    StreamLine(
                        agent_id=agent_id, start=start, end=end, line=line
                    ).model_dump_json()
                )
            websocket.send_text(StreamEof().model_dump_json())
            # The server closes once the trailing turns are committed; waiting for
            # it is what makes the second connect's welcome a commit assertion.
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets[SESSION_FILE] == total


def test_a_stop_over_the_hook_pipe_drains_the_socket() -> None:
    """End to end: the `Stop` hook's POST reaches the open socket as `finalize`, the
    client's `eof` commits the stopped turn, and the server closes the connection —
    the tail process's exit. The next welcome resuming past the turn is the
    protocol-visible proof of commit."""
    client, _ = stream_client()
    total = sum(len(line_bytes(record)) for record in TURN_ONE)

    with client:
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            websocket.receive_text()  # welcome
            for agent_id, start, end, line in frames(TURN_ONE):
                websocket.send_text(
                    StreamLine(
                        agent_id=agent_id, start=start, end=end, line=line
                    ).model_dump_json()
                )
            posted = client.post(
                CLAUDE_HOOK_PATH,
                json={
                    "hook_event_name": "Stop",
                    "session_id": SESSION_ID,
                    "prompt_id": "p1",
                },
                headers=AUTH,
            )
            assert posted.status_code == 200
            relayed = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(relayed, StreamFinalize)
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets[SESSION_FILE] == total


def test_a_stale_protocol_is_refused_loudly() -> None:
    client, _ = stream_client()
    with (
        client,
        client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello_json(protocol=99))
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 1008
    assert "protocol" in (disconnect.value.reason or "")


def test_a_driven_session_is_refused() -> None:
    """Octomate records the sessions it drives itself; streaming their transcripts
    would write those conversations a second time."""
    client, tentacle = stream_client()
    with client, tentacle.session_ingest.driving(SESSION_ID):
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            with pytest.raises(WebSocketDisconnect) as disconnect:
                websocket.receive_text()
    assert disconnect.value.code == 1008
    assert "drives" in (disconnect.value.reason or "")


def test_an_offset_gap_closes_for_resync() -> None:
    """A gap means frames were lost; the close makes the client reconnect and re-ask
    where to resume, instead of the server assembling a mis-framed turn."""
    client, _ = stream_client()
    with (
        client,
        client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello_json())
        websocket.receive_text()  # welcome
        websocket.send_text(StreamLine(start=5, end=20, line="{}").model_dump_json())
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 4000
    assert "offset gap" in (disconnect.value.reason or "")
