"""Remote Codex rollout ingest: raw framed lines over the stream endpoint, assembled
by the same tailer machinery the local pumps feed. Codex turns close on their own
`task_complete`/`turn_aborted` lines, so nothing commits at a connection boundary —
`eof` and a drop alike just return the registry slot — and every connect re-streams
from byte 0, because a rollout's head is load-bearing: `session_meta` carries the
thread id child classification checks against, and the parent's `sub_agent_activity`
lines are what link child runs to their spawning calls. The committed-turn guard is
what keeps the re-stream from duplicating anything."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.tentacles.codex import CODEX_HOOK_PATH, CODEX_STREAM_PATH
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
from octomate.config.agents import CodexConfig
from octomate.managers.user import UserManager
from octomate.schemas.runs import ExternalAgentRun
from octomate.schemas.thread import CODEX_NATIVE_ID, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex.tailer import CodexTranscriptTailer, TailState
from tests.agent.test_codex_native_ingest import (
    CHILD_THREAD_ID,
    CHILD_TURN_ONE,
    PARENT_TURN_ID,
    ROOT_THREAD_ID,
    SESSION_ID,
    answer_item,
    child_metadata,
    event,
    parent_metadata,
    subagent_activity,
)
from tests.support.agents import CODEX_MODELS
from tests.support.config import registered

SENDER = UserProfile(channel_user_id="lu", name="lu")

SECRET = SecretStr("the-hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}
# The rollout in the *client's* namespace: a label the server records, never opens.
CLIENT_PATH = Path("/laptop/.codex/sessions/2026/07/16") / (
    f"rollout-2026-07-16T10-00-00-{ROOT_THREAD_ID}.jsonl"
)
# The client's key for a sibling child rollout: its basename.
CHILD_KEY = f"rollout-2026-07-16T10-00-02-{CHILD_THREAD_ID}.jsonl"

TURN_A = [
    event(0, "task_started", turn_id="turn-a"),
    event(1, "user_message", message="first ask"),
    answer_item(2, "first answer"),
    event(3, "task_complete", turn_id="turn-a"),
]
TURN_B = [
    event(4, "task_started", turn_id="turn-b"),
    event(5, "user_message", message="second ask"),
    answer_item(6, "second answer"),
    event(7, "task_complete", turn_id="turn-b"),
]

Frame = tuple[str | None, int, int, str]


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def line_bytes(record: dict[str, object]) -> bytes:
    return json.dumps(record).encode() + b"\n"


def frames(
    records: list[dict[str, object]], *, agent_id: str | None = None, start: int = 0
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
    tailer: CodexTranscriptTailer, state: TailState, framed: list[Frame]
) -> None:
    for agent_id, start, end, line in framed:
        await tailer.feed_remote(state, agent_id, line, start, end)


def remote_tailer() -> tuple[Octomate, CodexTranscriptTailer]:
    octomate = Octomate()
    return octomate, CodexTranscriptTailer(
        octomate.conversations, octomate.thread_manager
    )


async def runs_of(octomate: Octomate) -> list[ExternalAgentRun]:
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    return [run for run in conversation.runs if isinstance(run, ExternalAgentRun)]


async def test_remote_feed_assembles_the_same_runs_as_a_local_pump() -> None:
    octomate, tailer = remote_tailer()

    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets == {SESSION_FILE: 0}
    assert tailer.sessions[SESSION_ID] is state  # so hooks see the session as covered
    await feed(tailer, state, frames([parent_metadata(), *TURN_A, *TURN_B]))
    tailer.detach_remote(state)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["turn-a", "turn-b"]
    a, b = runs
    # The client's offsets land as the runs' byte ranges: contiguous and gapless.
    assert a.start_offset == len(line_bytes(parent_metadata()))
    assert a.end_offset == b.start_offset
    assert [message.message_text for message in a.messages] == [
        "first ask",
        "first answer",
    ]
    assert SESSION_ID not in tailer.sessions  # registry slot reclaimed


async def test_a_reconnect_restreams_from_zero_and_committed_turns_dedup() -> None:
    """The dual of Claude's offset resume: a reconnect is told byte 0 — the head is
    load-bearing — and idempotency comes from the committed-turn guard instead."""
    octomate, tailer = remote_tailer()

    head = [parent_metadata(), *TURN_A]
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames(head))
    tailer.detach_remote(state)

    state, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets == {SESSION_FILE: 0}
    await feed(tailer, state, frames([*head, *TURN_B]))
    tailer.detach_remote(state)

    assert [run.id for run in await runs_of(octomate)] == ["turn-a", "turn-b"]


async def test_an_open_turn_never_commits_at_a_connection_boundary() -> None:
    """A turn without its `task_complete` is mid-flight when the connection ends.
    Committing it would plant its id in the committed-turn guard, where the completed
    version the next connect re-streams could never replace it — so it is dropped,
    exactly as a local follow's idle exit drops one."""
    octomate, tailer = remote_tailer()

    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames([parent_metadata(), TURN_A[0], TURN_A[1]]))
    tailer.detach_remote(state)
    assert await runs_of(octomate) == []

    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    await feed(tailer, state, frames([parent_metadata(), *TURN_A]))
    tailer.detach_remote(state)
    [run] = await runs_of(octomate)
    assert run.id == "turn-a"
    assert run.end_offset == sum(
        len(line_bytes(record)) for record in [parent_metadata(), *TURN_A]
    )


async def test_sibling_child_rollouts_stream_into_child_runs() -> None:
    """The client keys a child file by its basename — a label in its own namespace —
    and the server classifies it from its opening `session_meta`, binding it to the
    child thread it names and linking its runs to the parent's spawning call."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    await feed(
        tailer,
        state,
        frames(
            [
                parent_metadata(),
                event(0, "task_started", turn_id=PARENT_TURN_ID),
                subagent_activity(2, "call-spawn", kind="started"),
            ]
        ),
    )
    await feed(
        tailer,
        state,
        frames(
            [
                child_metadata(),
                event(3, "task_started", turn_id=CHILD_TURN_ONE),
                answer_item(4, "child answer"),
                event(5, "task_complete", turn_id=CHILD_TURN_ONE),
            ],
            agent_id=CHILD_KEY,
        ),
    )
    tailer.detach_remote(state)

    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "thread", SESSION_ID)
    )
    parent = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CODEX_NATIVE_ID
    )
    child = await octomate.conversations.ensure(
        thread.id,
        agent_tentacle_id=CODEX_NATIVE_ID,
        subagent_id=CHILD_THREAD_ID,
        parent_conversation_id=parent.id,
    )
    [run] = child.runs
    assert run.id == CHILD_TURN_ONE
    assert run.parent_run_id == PARENT_TURN_ID
    assert run.parent_tool_call_id == "call-spawn"


async def test_a_foreign_sibling_rollout_is_classified_once_and_dropped() -> None:
    """The tail ships every file the launcher spooled; one whose metadata is not a
    thread-spawned child of this session decides nothing and records nothing."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    await feed(tailer, state, frames([parent_metadata()]))
    await feed(
        tailer,
        state,
        frames(
            [
                child_metadata(source="fork"),
                event(3, "task_started", turn_id=CHILD_TURN_ONE),
                event(4, "task_complete", turn_id=CHILD_TURN_ONE),
            ],
            agent_id="rollout-foreign.jsonl",
        ),
    )
    tailer.detach_remote(state)

    assert state.subagents == {}
    assert await runs_of(octomate) == []


async def test_a_new_attach_replaces_a_lingering_registration() -> None:
    """A client that died without a close leaves its registration behind; the next
    connect replaces it, and the dead route's detach must not evict the
    replacement."""
    _, tailer = remote_tailer()
    stale, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)

    fresh, offsets = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    assert offsets == {SESSION_FILE: 0}
    assert tailer.sessions[SESSION_ID] is fresh
    tailer.detach_remote(stale)
    assert tailer.sessions[SESSION_ID] is fresh
    tailer.detach_remote(fresh)
    assert SESSION_ID not in tailer.sessions


async def test_a_stop_waits_for_the_stopped_turn_then_asks_the_drain() -> None:
    """`Stop` reaches a remote session as a per-turn drain, sequenced as a rescue
    rather than a cutoff: `stop_turn` waits for the stopped turn's own
    `task_complete` to commit it, and only then sets the `stop_event` the route's
    finalize relay watches — the client's drain then ships whatever a missed watch
    wake left behind, and the tail exits until the next prompt's launcher."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(SESSION_ID, CLIENT_PATH, SENDER)
    head = [parent_metadata(), *TURN_A[:-1]]
    await feed(tailer, state, frames(head))

    stopping = asyncio.create_task(tailer.stop_turn(SESSION_ID, "turn-a"))
    await asyncio.sleep(0)
    assert not state.stop_event.is_set()  # the drain waits for the commit

    start = sum(len(line_bytes(record)) for record in head)
    await feed(tailer, state, frames([TURN_A[-1]], start=start))
    await asyncio.wait_for(stopping, 2.0)
    assert state.stop_event.is_set()
    tailer.detach_remote(state)

    [run] = await runs_of(octomate)
    assert run.id == "turn-a"


def stream_client() -> tuple[TestClient, CodexTentacle]:
    config = registered(SECRET.get_secret_value())
    octomate = Octomate(config=config, users=UserManager(config.users))
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(models=set(CODEX_MODELS), permission_mode="deny_all"),
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
        with client.websocket_connect(CODEX_STREAM_PATH):
            pass
    assert denial.value.status_code == 401


def test_lines_flow_over_the_socket_and_eof_closes_it_cleanly() -> None:
    """End to end through the endpoint: hello/welcome, framed lines, eof, and the
    server's close. The next connect is welcomed at byte 0 again — Codex resumes by
    re-streaming, with the committed-turn guard as the dedup."""
    client, _ = stream_client()

    # One entered client, so both connects share a portal loop — the in-memory
    # database's connection is loop-bound, and two portals would strand it.
    with client:
        with client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 0}
            for agent_id, start, end, line in frames([parent_metadata(), *TURN_A]):
                websocket.send_text(
                    StreamLine(
                        agent_id=agent_id, start=start, end=end, line=line
                    ).model_dump_json()
                )
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

        with client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 0}


def test_a_stop_over_the_hook_pipe_drains_the_socket() -> None:
    """End to end: the `Stop` hook's POST waits for the streamed turn to commit,
    reaches the open socket as `finalize`, and the client's `eof` closes the
    connection — the tail process's exit. Codex resumes by re-streaming, so the
    next welcome is byte 0 again; the committed run is the proof of the drain."""
    client, _ = stream_client()

    with client:
        with client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            websocket.receive_text()  # welcome
            for agent_id, start, end, line in frames([parent_metadata(), *TURN_A]):
                websocket.send_text(
                    StreamLine(
                        agent_id=agent_id, start=start, end=end, line=line
                    ).model_dump_json()
                )
            posted = client.post(
                CODEX_HOOK_PATH,
                json={
                    "hook_event_name": "Stop",
                    "session_id": SESSION_ID,
                    "turn_id": "turn-a",
                },
                headers=AUTH,
            )
            assert posted.status_code == 200
            relayed = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(relayed, StreamFinalize)
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

        with client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 0}


def test_a_stale_protocol_is_refused_loudly() -> None:
    client, _ = stream_client()
    with client, client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
        websocket.send_text(hello_json(protocol=99))
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 1008
    assert "protocol" in (disconnect.value.reason or "")


def test_a_driven_session_is_refused() -> None:
    """Octomate records the sessions it drives itself; streaming their rollouts
    would write those conversations a second time."""
    client, tentacle = stream_client()
    with client, tentacle.session_ingest.driving(SESSION_ID):
        with client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            with pytest.raises(WebSocketDisconnect) as disconnect:
                websocket.receive_text()
    assert disconnect.value.code == 1008
    assert "drives" in (disconnect.value.reason or "")


def test_an_offset_gap_closes_for_resync() -> None:
    """A gap means frames were lost; the close makes the client reconnect and re-ask
    where to resume, instead of the server assembling a mis-framed turn."""
    client, _ = stream_client()
    with client, client.websocket_connect(CODEX_STREAM_PATH, headers=AUTH) as websocket:
        websocket.send_text(hello_json())
        websocket.receive_text()  # welcome
        websocket.send_text(StreamLine(start=5, end=20, line="{}").model_dump_json())
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 4000
    assert "offset gap" in (disconnect.value.reason or "")
