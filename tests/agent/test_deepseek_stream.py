"""Remote dsh event ingest: framed history entries over the stream endpoint,
assembled by the same tailer the feed tests drive directly. dsh turns close on
their own `turn/end` lines, so nothing commits at a connection boundary, and a
reconnect resumes at the seq after the committed floor — the offsets are event
seqs, not bytes, because the client reads its dsh gateway rather than a file."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli.tentacles.deepseek import DEEPSEEK_HOOK_PATH, DEEPSEEK_STREAM_PATH
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
from starlette.websockets import WebSocketDisconnect

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.agents import DeepseekConfig
from octomate.database import async_session
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import Thread
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.types.json import JsonObject
from tests.agent.test_deepseek_native_ingest import (
    LOG_LABEL,
    SESSION_ID,
    turn_events,
)
from tests.support.managers import a_thread
from tests.support.users import a_api_key, a_user, auth_config

SECRET = SecretStr("the-hook-secret")
AUTH = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


def stream_client() -> tuple[TestClient, DeepseekTentacle]:
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    tentacle = octomate.connect(
        DeepseekTentacle("deepseek", octomate, config=DeepseekConfig())
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


def test_a_session_already_used_by_the_sdk_streams_as_external() -> None:
    client, tentacle = stream_client()

    async def register_sdk_session() -> Conversation:
        octomate = tentacle.octomate
        sdk_conversation = await octomate.conversations.ensure(
            await a_thread(), agent_tentacle_id="deepseek"
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
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            [state] = tentacle.session_tailer.sessions.values()
            assert state.conversation is not None
            assert state.conversation.id != sdk.id
            assert state.conversation.agent_tentacle_id == "deepseek-native"
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()


@pytest.mark.parametrize("other_tentacle", [False, True])
async def test_driven_sessions_skip_hooks_and_streams_until_released(
    other_tentacle: bool,
) -> None:
    client, tentacle = stream_client()
    driver = (
        tentacle.octomate.connect(
            DeepseekTentacle(
                "other-deepseek", tentacle.octomate, config=DeepseekConfig()
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
            for event in ("UserPromptSubmit", "Stop"):
                posted = client.post(
                    DEEPSEEK_HOOK_PATH,
                    json={"hook_event_name": event, "session_id": SESSION_ID},
                    headers=AUTH,
                )
                assert posted.status_code == 200
                assert posted.json() == {}
            with client.websocket_connect(
                DEEPSEEK_STREAM_PATH, headers=AUTH
            ) as websocket:
                websocket.send_text(hello_json())
                with pytest.raises(WebSocketDisconnect) as disconnect:
                    websocket.receive_text()
            assert disconnect.value.code == 1008
            assert "drives" in (disconnect.value.reason or "")
            assert tentacle.session_tailer.sessions == {}
            assert tentacle.native_sessions == {}
            assert tentacle.session_ingest.tasks == set()
            assert client.portal is not None
            assert client.portal.call(native_threads) == []

        assert driver.driven_sessions == {}
        posted = client.post(
            DEEPSEEK_HOOK_PATH,
            json={"hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID},
            headers=AUTH,
        )
        assert posted.status_code == 200
        assert len(client.portal.call(native_threads)) == 1
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
            websocket.send_text(hello_json())
            welcome = server_message_adapter.validate_json(websocket.receive_text())
            assert isinstance(welcome, StreamWelcome)
            websocket.send_text(StreamEof().model_dump_json())
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()


@pytest.mark.parametrize(
    "next_message",
    [StreamEof(), StreamLine(start=0, end=1, line="{}")],
    ids=["eof", "line"],
)
async def test_an_accepted_stream_continues_when_the_session_becomes_driven(
    next_message: StreamEof | StreamLine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, tentacle = stream_client()
    fed = AsyncMock()
    monkeypatch.setattr(tentacle.session_tailer, "feed_remote", fed)

    with client:
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as websocket:
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

    assert fed.await_count == int(isinstance(next_message, StreamLine))


def test_native_session_counts_overlap_and_release_on_eof_and_disconnect() -> None:
    client, tentacle = stream_client()
    with client:
        with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as first:
            first.send_text(hello_json())
            welcome = server_message_adapter.validate_json(first.receive_text())
            assert isinstance(welcome, StreamWelcome)
            assert tentacle.native_sessions == {SESSION_ID: 1}
            with client.websocket_connect(DEEPSEEK_STREAM_PATH, headers=AUTH) as second:
                second.send_text(hello_json())
                welcome = server_message_adapter.validate_json(second.receive_text())
                assert isinstance(welcome, StreamWelcome)
                assert tentacle.native_sessions == {SESSION_ID: 2}
                second.send_text(StreamEof().model_dump_json())
                with pytest.raises(WebSocketDisconnect) as disconnect:
                    second.receive_text()
                assert disconnect.value.code == 1000
            assert tentacle.native_sessions == {SESSION_ID: 1}
        assert tentacle.native_sessions == {}


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
