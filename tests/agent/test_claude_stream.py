"""Transcript ingest over the stream endpoint — the only assembler: raw framed
lines fed per file. The server owns every cursor — a connect is answered with where
each file resumes, computed from the committed runs — and a connection that drops
without `eof` leaves its open turns for the next connect to re-stream."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from anyio.abc import ObjectSendStream
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
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from octomate import Octomate
from octomate.config import ClaudeCodeConfig, OctomateConfig
from octomate.database import async_session
from octomate.nerve import Impulse
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer, TailState
from octomate.types.json import JsonObject
from tests.agent.test_claude_tailer import (
    AGENT_ID,
    SESSION_ADDRESS,
    SESSION_ID,
    SUB_TURN_ONE,
    TURN_ONE,
    TURN_TWO,
    agent_spawn_records,
    line_bytes,
    runs_of,
    subagent_runs_of,
)
from tests.support.managers import a_thread
from tests.support.nerve import held
from tests.support.users import a_api_key, a_user, auth_config

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
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    return octomate, ClaudeTranscriptTailer(octomate.conversations, octomate.threads)


async def test_remote_feed_assembles_the_same_runs_as_a_local_tail(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()

    state, offsets = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
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


async def test_a_reconnect_is_told_to_resume_from_the_committed_runs(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()

    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    await feed(tailer, state, frames(TURN_ONE))
    await tailer.finish_remote(state)
    (p1,) = await runs_of(octomate)

    state, offsets = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    assert offsets[SESSION_FILE] == p1.end_offset
    await feed(tailer, state, frames(TURN_TWO, start=p1.end_offset or 0))
    await tailer.finish_remote(state)

    runs = await runs_of(octomate)
    assert [run.id for run in runs] == ["p1", "p2"]
    assert runs[1].start_offset == p1.end_offset  # no gap, no overlap


async def test_a_drop_without_eof_leaves_open_turns_for_the_next_connect(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    """Mid-turn bytes are never provably complete when the socket dies, so nothing
    commits — committing a partial turn would advance the resume mark past bytes no
    run covers, stranding the rest of the turn where no recovery could reach it."""
    octomate, tailer = remote_tailer()

    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    await feed(tailer, state, frames(TURN_ONE))  # open turn: no closing prompt yet
    tailer.detach_remote(state)

    assert await runs_of(octomate) == []
    state, offsets = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    assert offsets == {SESSION_FILE: 0}  # nothing committed: re-stream from the top
    await feed(tailer, state, frames(TURN_ONE + TURN_TWO))
    await tailer.finish_remote(state)
    assert [run.id for run in await runs_of(octomate)] == ["p1", "p2"]


async def test_finalize_relays_through_stop_event_and_waits_for_the_drain(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    """`SessionEnd` reaches a remote session as its `stop_event` — the stream route
    sends the client `finalize` on it — and `finalize` returns once `finish_remote`
    has committed, not after its timeout."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    await feed(tailer, state, frames(TURN_ONE))

    finalizing = asyncio.create_task(tailer.finalize(SESSION_ID))
    await asyncio.wait_for(state.stop_event.wait(), 2)
    # The route's reaction, compressed: the client drains (nothing new) and eofs.
    await tailer.finish_remote(state)
    await asyncio.wait_for(finalizing, 2)  # well under REMOTE_DRAIN_TIMEOUT

    assert [run.id for run in await runs_of(octomate)] == ["p1"]


async def test_a_stop_drains_the_remote_tail_and_commits_the_stopped_turn(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    """`Stop` reaches a remote session as a per-turn drain: `stop_turn` sets the
    `stop_event` the route's relay watches, and the `eof` answering the relayed
    `finalize` commits the stopped turn — the next prompt no longer gates it."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    await feed(tailer, state, frames(TURN_ONE))  # open turn: no closing prompt yet

    await tailer.stop_turn(SESSION_ID, "p1")
    assert state.stop_event.is_set()  # what the route's finalize relay watches
    await tailer.finish_remote(state)  # the client's drain answered eof

    (p1,) = await runs_of(octomate)
    assert p1.id == "p1"
    assert p1.end_offset is not None
    assert SESSION_ID not in tailer.sessions  # the tail is done; slot reclaimed


async def test_a_prompt_queued_into_the_drain_stays_open_for_the_next_connect(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    """A queued message can land between `Stop` and the drain's `eof`, so its prompt
    line ships with the drain and opens the next turn mid-drain. Only the stopped
    turn may commit: a partial next turn committed here would hold its id against the
    complete version forever."""
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    await feed(tailer, state, frames(TURN_ONE))
    await tailer.stop_turn(SESSION_ID, "p1")

    total = sum(len(line_bytes(record)) for record in TURN_ONE)
    await feed(tailer, state, frames(TURN_TWO[:1], start=total))  # p2's prompt line
    await tailer.finish_remote(state)

    (p1,) = await runs_of(octomate)  # p1 closed on its own boundary; p2 not committed
    assert p1.id == "p1"

    state, offsets = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    assert offsets[SESSION_FILE] == p1.end_offset  # p2's line re-streams whole
    tailer.detach_remote(state)


async def test_subagent_lines_stream_into_child_runs(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )

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
    state, offsets = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    assert offsets[AGENT_ID] == child.end_offset
    tailer.detach_remote(state)


async def test_a_new_attach_replaces_a_lingering_registration(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    """A client that died without a close leaves its registration behind; the next
    connect replaces it, and the dead route's detach must not evict the
    replacement."""
    _, tailer = remote_tailer()
    stale, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )

    fresh, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, impulses=tail_impulses
    )
    assert tailer.sessions[SESSION_ID] is fresh
    tailer.detach_remote(stale)
    assert tailer.sessions[SESSION_ID] is fresh
    tailer.detach_remote(fresh)
    assert SESSION_ID not in tailer.sessions


def stream_client() -> tuple[TestClient, ClaudeCodeTentacle]:
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    tentacle = octomate.connect(
        ClaudeCodeTentacle(
            "claude",
            octomate,
            config=ClaudeCodeConfig(),
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            mcp=octomate.mcp,
        )
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        user = await a_user()
        await a_api_key(user, SECRET.get_secret_value())
        yield

    octomate.router.lifespan_context = lifespan
    return TestClient(octomate), tentacle


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
    client, tentacle = stream_client()
    total = sum(len(line_bytes(record)) for record in TURN_ONE + TURN_TWO)

    # One entered client, so both connects share a portal loop — the in-memory
    # database's connection is loop-bound, and two portals would strand it.
    key = ThreadKey.from_address(SESSION_ADDRESS)
    with client, tentacle.octomate.nerve.subscribe(key) as live:
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 0}
            assert tentacle.native_sessions == {SESSION_ID: 1}
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

        assert tentacle.native_sessions == {}
        impulses = held(live)
        assert [
            impulse.event.event_kind
            for impulse in impulses
            if impulse.event.event_kind in {"run_started", "run_ended"}
        ] == ["run_started", "run_ended"] * 2
        assert all(impulse.address == SESSION_ADDRESS for impulse in impulses)
        assert all(impulse.origin == key for impulse in impulses)
        assert not tentacle.octomate.awakes
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets[SESSION_FILE] == total
            assert tentacle.native_sessions == {SESSION_ID: 1}

        assert tentacle.native_sessions == {}


@pytest.mark.parametrize("clean", [False, True], ids=["disconnect", "eof"])
def test_native_counts_hold_overlapping_streams_until_both_leave(clean: bool) -> None:
    client, tentacle = stream_client()

    with client:
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as first:
            first.send_text(hello_json())
            first.receive_text()
            assert tentacle.native_sessions == {SESSION_ID: 1}
            with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as second:
                second.send_text(hello_json())
                second.receive_text()
                assert tentacle.native_sessions == {SESSION_ID: 2}
                if clean:
                    second.send_text(StreamEof().model_dump_json())
                    with pytest.raises(WebSocketDisconnect):
                        second.receive_text()
            assert tentacle.native_sessions == {SESSION_ID: 1}
        assert tentacle.native_sessions == {}


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


def test_a_session_already_used_by_the_sdk_streams_as_external() -> None:
    client, tentacle = stream_client()

    async def register_sdk_session() -> Conversation:
        octomate = tentacle.octomate
        sdk_conversation = await octomate.conversations.ensure(
            await a_thread(), agent_tentacle_id="claude"
        )
        await octomate.conversations.record_agent_run(
            sdk_conversation,
            run_id="sdk-run",
            messages=[ModelRequest(parts=[UserPromptPart(content="SDK prompt")])],
            external_id=SESSION_ID,
        )
        return sdk_conversation

    with client:
        assert client.portal is not None
        sdk = client.portal.call(register_sdk_session)
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            [state] = tentacle.session_tailer.sessions.values()
            assert state.conversation is not None
            assert state.conversation.id != sdk.id
            assert state.conversation.agent_tentacle_id == "claude-native"
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()


@pytest.mark.parametrize("other_tentacle", [False, True])
async def test_driven_sessions_are_accepted_by_both_ingest_endpoints(
    other_tentacle: bool,
) -> None:
    client, tentacle = stream_client()
    driver = (
        tentacle.octomate.connect(
            ClaudeCodeTentacle(
                "other-claude",
                tentacle.octomate,
                config=ClaudeCodeConfig(),
                commands=tentacle.octomate.commands,
                projects=tentacle.octomate.projects,
                threads=tentacle.octomate.threads,
                conversations=tentacle.octomate.conversations,
                deferred_actions=tentacle.octomate.deferred_actions,
                workspaces=tentacle.octomate.workspaces,
                users=tentacle.octomate.users,
                bearers=tentacle.octomate.bearers,
                mcp=tentacle.octomate.mcp,
                files=tentacle.octomate.files,
            )
        )
        if other_tentacle
        else tentacle
    )

    async def native_threads() -> list[Thread]:
        async with async_session() as session:
            return list(await session.list(Thread, limit=None, order_bys=[]))

    with client:
        async with driver.driving(SESSION_ID):
            posted = client.post(
                CLAUDE_HOOK_PATH,
                json={
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": SESSION_ID,
                    "prompt_id": "p1",
                    "prompt": "native prompt",
                },
                headers=AUTH,
            )
            assert posted.status_code == 200
            assert posted.json() == {}
            with client.websocket_connect(
                CLAUDE_STREAM_PATH, headers=AUTH
            ) as websocket:
                websocket.send_text(hello_json())
                welcome = server_message_adapter.validate_json(websocket.receive_text())
                assert isinstance(welcome, StreamWelcome)
                websocket.send_text(StreamEof().model_dump_json())
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_text()
            assert tentacle.native_sessions == {}
            assert client.portal is not None
            assert len(client.portal.call(native_threads)) == 1

        posted = client.post(
            CLAUDE_HOOK_PATH,
            json={
                "hook_event_name": "UserPromptSubmit",
                "session_id": SESSION_ID,
                "prompt_id": "p2",
                "prompt": "native prompt",
            },
            headers=AUTH,
        )
        assert posted.status_code == 200
        assert len(client.portal.call(native_threads)) == 1
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert tentacle.native_sessions == {SESSION_ID: 1}
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
        assert tentacle.native_sessions == {}


@pytest.mark.parametrize(
    "next_message",
    [StreamEof(), StreamLine(start=0, end=1, line="{}")],
    ids=["eof", "line"],
)
async def test_an_attached_stream_keeps_its_native_ownership(
    next_message: StreamEof | StreamLine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, tentacle = stream_client()
    fed = AsyncMock()
    finished = AsyncMock(wraps=tentacle.session_tailer.finish_remote)
    monkeypatch.setattr(tentacle.session_tailer, "feed_remote", fed)
    monkeypatch.setattr(tentacle.session_tailer, "finish_remote", finished)

    with client:
        with client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert tentacle.native_sessions == {SESSION_ID: 1}
            async with tentacle.driving(SESSION_ID):
                websocket.send_text(next_message.model_dump_json())
                if isinstance(next_message, StreamLine):
                    websocket.send_text(StreamEof().model_dump_json())
                with pytest.raises(WebSocketDisconnect) as disconnect:
                    websocket.receive_text()
                assert disconnect.value.code == 1000
        assert tentacle.session_tailer.sessions == {}
        assert tentacle.native_sessions == {}

    if isinstance(next_message, StreamLine):
        fed.assert_awaited_once()
    else:
        fed.assert_not_awaited()
    finished.assert_awaited_once()


@pytest.mark.parametrize("failure", ["attach_remote", "feed_remote", "close_turn"])
def test_stream_failure_closes_socket_and_releases_session(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, tentacle = stream_client()
    monkeypatch.setattr(
        tentacle.session_tailer,
        failure,
        AsyncMock(side_effect=RuntimeError("storage failed")),
    )
    with (
        client,
        client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello_json())
        if failure != "attach_remote":
            websocket.receive_text()
            message = (
                StreamLine(start=0, end=1, line="{}")
                if failure == "feed_remote"
                else StreamEof()
            )
            websocket.send_text(message.model_dump_json())
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
        assert disconnect.value.code == 1011
        assert disconnect.value.reason == "transcript sync failed"

    assert tentacle.session_tailer.sessions == {}
    assert tentacle.native_sessions == {}
    assert "storage failed" in caplog.text


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


def test_a_blocked_parent_does_not_stop_a_child_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, tentacle = stream_client()
    child_done = asyncio.Event()
    seen: list[str] = []

    async def consume(
        state: TailState, agent_id: str | None, raw: str, start: int, end: int
    ) -> None:
        if agent_id is None:
            await asyncio.wait_for(child_done.wait(), timeout=2)
        seen.append(raw)
        if agent_id is not None:
            child_done.set()

    monkeypatch.setattr(tentacle.session_tailer, "feed_remote", consume)
    monkeypatch.setattr(tentacle, "keep_transcript", AsyncMock(return_value=0))
    with client, client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as socket:
        socket.send_text(hello_json())
        socket.receive_text()
        for agent_id, line, start in (
            (None, "parent 1", 0),
            (None, "parent 2", 9),
            ("child", "child 1", 0),
            ("child", "child 2", 8),
        ):
            socket.send_text(
                StreamLine(
                    agent_id=agent_id, line=line, start=start, end=start + len(line) + 1
                ).model_dump_json()
            )
        socket.send_text(StreamEof().model_dump_json())
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    assert [line for line in seen if line.startswith("parent")] == [
        "parent 1",
        "parent 2",
    ]
    assert [line for line in seen if line.startswith("child")] == ["child 1", "child 2"]
    assert seen.index("child 1") < seen.index("parent 1")


async def test_child_finishing_before_parent_metadata_keeps_its_hierarchy(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, tail_impulses
    )
    await feed(tailer, state, frames(SUB_TURN_ONE, agent_id=AGENT_ID))
    await tailer.close_subagent_turn(state, state.subagents[AGENT_ID])
    [child] = await subagent_runs_of(octomate)
    assert child.parent_tool_call_id is None
    await feed(tailer, state, frames([TURN_ONE[0], *agent_spawn_records("p1", 2)]))
    [child] = await subagent_runs_of(octomate)
    assert child.parent_run_id == "p1"
    assert child.parent_tool_call_id == f"toolu-agent-{AGENT_ID}"
    tailer.detach_remote(state)


def test_worker_failure_after_eof_never_finalizes_partial_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, tentacle = stream_client()
    finished = AsyncMock()
    eof_received = asyncio.Event()
    receive = WebSocket.receive_text

    async def observe_eof(socket: WebSocket) -> str:
        raw = await receive(socket)
        if raw == StreamEof().model_dump_json():
            eof_received.set()
        return raw

    async def fail_after_eof(
        state: TailState, agent_id: str | None, raw: str, start: int, end: int
    ) -> None:
        await eof_received.wait()
        raise RuntimeError("commit failed")

    monkeypatch.setattr(WebSocket, "receive_text", observe_eof)
    monkeypatch.setattr(tentacle.session_tailer, "feed_remote", fail_after_eof)
    monkeypatch.setattr(tentacle.session_tailer, "finish_remote", finished)
    with client, client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as socket:
        socket.send_text(hello_json())
        socket.receive_text()
        socket.send_text(
            StreamLine(agent_id=None, line="line", start=0, end=5).model_dump_json()
        )
        socket.send_text(StreamEof().model_dump_json())
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
        assert closed.value.code == 1011
    finished.assert_not_awaited()
    assert tentacle.session_tailer.sessions == {}


async def test_child_commit_does_not_take_the_parent_lock(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, tail_impulses
    )
    await feed(tailer, state, frames(SUB_TURN_ONE, agent_id=AGENT_ID))
    async with tailer.locks.hold(SESSION_ID):
        await asyncio.wait_for(
            tailer.close_subagent_turn(state, state.subagents[AGENT_ID]), timeout=1
        )
    [run] = await subagent_runs_of(octomate)
    assert run.parent_run_id == "p1"
    assert run.end_offset is not None
    tailer.detach_remote(state)


async def test_resumed_child_uses_the_spawning_call_of_its_own_parent_turn(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, SENDER, SESSION_ADDRESS, tail_impulses
    )
    # The parent is ahead of the child: both invocation records arrived before
    # the child's first turn. Each invocation must retain its own call identity.
    offset = 0
    for prompt_id in ("p1", "p2"):
        for record in agent_spawn_records(prompt_id, 2):
            raw = (
                line_bytes(record)
                .decode()
                .replace(f"toolu-agent-{AGENT_ID}", f"call-{prompt_id}")
            )
            end = offset + len(raw.encode())
            await tailer.feed_remote(state, None, raw.rstrip("\n"), offset, end)
            offset = end
    await feed(tailer, state, frames(SUB_TURN_ONE, agent_id=AGENT_ID))
    await tailer.close_subagent_turn(state, state.subagents[AGENT_ID])
    [child] = await subagent_runs_of(octomate)
    assert child.parent_run_id == "p1"
    assert child.parent_tool_call_id == "call-p1"
    tailer.detach_remote(state)


async def test_transcript_ledger_uses_the_authenticated_stream_sender(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()
    user = await a_user("tail-owner")
    sender = await octomate.users.native_profile("claude-native", user.username)
    assert sender is not None
    state, _ = await tailer.attach_remote(
        SESSION_ID, CLIENT_PATH, sender, SESSION_ADDRESS, tail_impulses
    )
    await feed(tailer, state, frames(TURN_ONE))
    await tailer.finish_remote(state)
    thread = await octomate.threads.ensure(
        ThreadKey("claude-native", "thread", SESSION_ID)
    )
    page = await octomate.threads.messages_page(thread.id, limit=100)
    assert len(page.items) == 2
    profile = await octomate.users.profile("claude-native", sender.channel_user_id)
    assert profile is not None
    assert all(row.sender_id == profile.id for row in page.items)
    prompt = next(row for row in page.items if row.direction == "inbound")
    assert prompt.user_id == sender.channel_user_id
    owner = await octomate.users.owner(profile)
    assert owner is not None
    assert owner.id == user.id


async def test_opening_transcript_prompt_grants_access_and_replays_once(
    tail_impulses: ObjectSendStream[Impulse],
) -> None:
    octomate, tailer = remote_tailer()
    user = await a_user("tail-owner")
    sender = await octomate.users.native_profile("claude-native", user.username)
    assert sender is not None
    key = ThreadKey("claude-native", "thread", SESSION_ID)

    for _ in range(2):
        state, offsets = await tailer.attach_remote(
            SESSION_ID,
            CLIENT_PATH,
            sender,
            key.address(sender.channel_user_id),
            tail_impulses,
        )
        assert offsets == {SESSION_FILE: 0}
        await feed(tailer, state, frames(TURN_ONE[:1]))
        thread = await octomate.threads.get(key, user_id=user.id)
        assert thread is not None
        assert len(thread.messages) == 1
        assert thread.messages[0].platform_message_id == "p1"
        assert thread.messages[0].message_text == "list the files"
        assert await runs_of(octomate) == []
        tailer.detach_remote(state)


def test_disconnect_finishes_the_current_line_before_stopping_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, tentacle = stream_client()
    started = threading.Event()
    disconnected = asyncio.Event()
    completed: list[str] = []
    receive = WebSocket.receive_text

    async def observe_disconnect(socket: WebSocket) -> str:
        try:
            return await receive(socket)
        except WebSocketDisconnect:
            disconnected.set()
            raise

    async def consume(
        state: TailState, agent_id: str | None, raw: str, start: int, end: int
    ) -> None:
        started.set()
        await disconnected.wait()
        # Give the worker group's cancellation a checkpoint inside the line.
        await asyncio.sleep(0)
        completed.append(raw)

    monkeypatch.setattr(WebSocket, "receive_text", observe_disconnect)
    monkeypatch.setattr(tentacle.session_tailer, "feed_remote", consume)
    monkeypatch.setattr(tentacle, "keep_transcript", AsyncMock(return_value=0))
    with client, client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as socket:
        socket.send_text(hello_json())
        socket.receive_text()
        socket.send_text(
            StreamLine(agent_id=None, line="line", start=0, end=5).model_dump_json()
        )
        assert started.wait(timeout=2)
        socket.close()
    assert completed == ["line"]
    assert tentacle.session_tailer.sessions == {}
