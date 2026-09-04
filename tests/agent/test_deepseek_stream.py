"""Remote dsh event ingest: framed history entries over the stream endpoint,
assembled by the same tailer the feed tests drive directly. dsh turns close on
their own `turn/end` lines, so nothing commits at a connection boundary, and a
reconnect resumes at the seq after the committed floor — the offsets are event
seqs, not bytes, because the client reads its dsh gateway rather than a file."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.deepseek import DEEPSEEK_HOOK_PATH, DEEPSEEK_STREAM_PATH
from octomate_cli.stream import (
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
from octomate.config.agents import DeepseekConfig
from octomate.managers.user import UserManager
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.types.json import JsonObject
from tests.agent.test_deepseek_native_ingest import (
    LOG_LABEL,
    SESSION_ID,
    turn_events,
)
from tests.support.agents import DEEPSEEK_MODELS
from tests.support.config import registered

SECRET = SecretStr("the-hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def stream_client() -> tuple[TestClient, DeepseekTentacle]:
    config = registered(SECRET.get_secret_value())
    octomate = Octomate(config=config, users=UserManager(config.users))
    tentacle = DeepseekTentacle(
        "deepseek",
        octomate,
        config=DeepseekConfig(models=set(DEEPSEEK_MODELS)),
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
        transcript_path=str(LOG_LABEL),
        cwd="/work/repo",
    ).model_dump_json()


def line_json(event: JsonObject) -> str:
    seq = event["seq"]
    assert isinstance(seq, int)
    return StreamLine(
        start=seq, end=seq + 1, line=json.dumps({"event": event})
    ).model_dump_json()


def test_the_stream_authenticates_like_the_hook_routers() -> None:
    client, _ = stream_client()
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(DEEPSEEK_STREAM_PATH):
            pass
    assert denial.value.status_code == 401


def test_entries_flow_over_the_socket_and_the_next_connect_resumes() -> None:
    """End to end through the endpoint: hello/welcome, framed entries, eof, and
    the server's close. The next connect is welcomed at the seq after the
    committed turn — the client re-reads its gateway from there."""
    client, _ = stream_client()

    with client:
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 0}
            for event in turn_events(1, 0, "streamed ask", "streamed answer"):
                websocket.send_text(line_json(event))
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()

        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert welcome.offsets == {SESSION_FILE: 4}


def test_a_stop_over_the_hook_pipe_drains_the_socket() -> None:
    """End to end: the `Stop` hook returns at once (dsh's stopping seam blocks
    on it), the detached settle waits for the streamed close to commit, and the
    finalize reaches the socket for the client's final drain and `eof`."""
    client, _ = stream_client()

    with client:
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            websocket.receive_text()  # welcome
            events = turn_events(1, 0, "stopping ask", "stopped answer")
            for event in events[:-1]:
                websocket.send_text(line_json(event))
            posted = client.post(
                DEEPSEEK_HOOK_PATH,
                json={"hook_event_name": "Stop", "session_id": SESSION_ID},
                headers=AUTH,
            )
            assert posted.status_code == 200
            # The seam released and dsh flushed the close; the stream ships it.
            websocket.send_text(line_json(events[-1]))
            relayed = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(relayed, StreamFinalize)
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()


def test_a_stale_protocol_is_refused_loudly() -> None:
    client, _ = stream_client()
    with (
        client,
        client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello_json(protocol=99))
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 1008
    assert "protocol" in (disconnect.value.reason or "")


def test_a_driven_session_is_refused() -> None:
    """Octomate records the sessions it drives itself; streaming their events
    would write those conversations a second time."""
    client, tentacle = stream_client()
    with client, tentacle.session_ingest.driving(SESSION_ID):
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            with pytest.raises(WebSocketDisconnect) as disconnect:
                websocket.receive_text()
    assert disconnect.value.code == 1008
    assert "drives" in (disconnect.value.reason or "")


def test_a_seq_gap_closes_for_resync() -> None:
    """A gap means entries were lost; the close makes the client reconnect and
    re-ask where to resume, instead of the server assembling a mis-framed turn."""
    client, _ = stream_client()
    with (
        client,
        client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello_json())
        websocket.receive_text()  # welcome
        websocket.send_text(StreamLine(start=5, end=6, line="{}").model_dump_json())
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 4000
    assert "seq gap" in (disconnect.value.reason or "")


def test_a_labeled_line_is_refused() -> None:
    """A dsh session streams as one event sequence; a line keyed to a sibling
    file is another agent's protocol."""
    client, _ = stream_client()
    with (
        client,
        client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello_json())
        websocket.receive_text()  # welcome
        websocket.send_text(
            StreamLine(agent_id="agent-1", start=0, end=1, line="{}").model_dump_json()
        )
        with pytest.raises(WebSocketDisconnect) as disconnect:
            websocket.receive_text()
    assert disconnect.value.code == 1008
