"""The dsh tail client's reading half: gateway paging back to the cursor, and
the one safety rule it applies — never ship a trailing turn whose interrupted
close may be the gateway reader's in-memory synthesis for a turn some other
process is still writing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from octomate_cli.streaming import deepseek
from octomate_cli.streaming.deepseek import (
    DshCompatibilityError,
    DshHistoryClient,
    new_entries,
    seq_of,
    session_origin,
    shippable,
)
from octomate_protocol.deepseek import RemoteItem, RemoteSessionFollow
from pydantic import JsonValue
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

DSH_URL = "http://127.0.0.1:3080"
SESSION_ID = "session-native-0001"


def event(seq: int, kind: str, data: JsonValue = None) -> dict[str, JsonValue]:
    return {"type": kind, "seq": seq, "time": 1.0 + seq, "data": data}


def entry(seq: int, kind: str, data: JsonValue = None) -> dict[str, JsonValue]:
    return {"event": event(seq, kind, data)}


def turn(base: int, *, reason: str = "completed") -> list[dict[str, JsonValue]]:
    # The log's real per-turn order: the turn opens first, the prompt splices
    # in after it.
    return [
        entry(base, "turn/start", {"turn": 1}),
        entry(base + 1, "user/message", {"content": [{"type": "text", "text": "ask"}]}),
        entry(
            base + 2, "assistant/chunk", {"chunk": {"type": "text-delta", "text": "a"}}
        ),
        entry(base + 3, "turn/end", {"turn": 1, "reason": {"kind": reason}}),
    ]


class PagingGateway(DshHistoryClient):
    """`session.history` served in fixed windows counted back from the tail,
    `beforeSeq` exclusive — the paging shape the real gateway answers with."""

    def __init__(self, entries: list[dict[str, JsonValue]], page_size: int) -> None:
        self.entries = entries
        self.page_size = page_size
        self.calls: list[dict[str, JsonValue]] = []

    def snapshot(self, session_id: str) -> dict[str, JsonValue]:
        self.calls.append({"method": "session/follow"})
        return {
            "type": "snapshot",
            "cursor": len(self.entries) - 1,
            "records": list(self.entries[-self.page_size :]),
            "hasMore": len(self.entries) > self.page_size,
        }

    def rpc(self, method: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]:
        assert method == "session/page"
        payload = args["request"]
        assert isinstance(payload, dict)
        assert payload["throughSeq"] == len(self.entries) - 1
        self.calls.append({"method": method, **payload})
        before = payload.get("beforeSeq")
        assert isinstance(before, int)
        window = [
            item
            for item in self.entries
            if (seq := seq_of(item)) is not None and seq < before
        ]
        page: list[JsonValue] = list(window[-self.page_size :])
        return {"records": page, "hasMore": len(window) > len(page)}


async def test_snapshot_larger_than_one_mebibyte_reads_every_record() -> None:
    records = [
        entry(seq, "assistant/chunk", {"chunk": {"text": "x" * 2600}})
        for seq in range(643)
    ]
    snapshot: dict[str, JsonValue] = {
        "type": "snapshot",
        "cursor": len(records) - 1,
        "records": list(records),
        "hasMore": False,
    }
    message = RemoteItem(
        type="item", stream_id=SESSION_ID, value=snapshot
    ).model_dump_json(by_alias=True)
    assert len(message.encode()) > 2**20

    async def gateway(websocket: ServerConnection) -> None:
        request = RemoteSessionFollow.model_validate_json(await websocket.recv())
        assert request.stream_id == SESSION_ID
        await websocket.send(message)
        await websocket.wait_closed()

    async with asyncio.timeout(5), serve(gateway, "127.0.0.1", 0) as host:
        port = next(iter(host.sockets)).getsockname()[1]
        client = DshHistoryClient(f"http://127.0.0.1:{port}")
        entries = await asyncio.to_thread(new_entries, client, SESSION_ID, 0)

    assert entries == records


@pytest.mark.parametrize("code", [1002, 1003, 1007, 1009])
@pytest.mark.parametrize("echoed", [False, True])
async def test_local_protocol_close_surfaces_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int, echoed: bool
) -> None:
    frame = Close(code, "local protocol failure")
    closed = ConnectionClosedError(
        frame if echoed else None, frame, False if echoed else None
    )
    stream = AsyncMock(side_effect=[closed, True])
    sleep = AsyncMock()
    monkeypatch.setattr(deepseek, "stream_session", stream)
    monkeypatch.setattr(deepseek.asyncio, "sleep", sleep)

    with pytest.raises(ConnectionClosedError, match="local protocol failure"):
        await deepseek.run_tail(
            "ws://octomate.example/stream",
            SESSION_ID,
            tmp_path / "session",
            str(tmp_path),
            "test-token",
            DSH_URL,
        )

    stream.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.parametrize(
    ("received", "sent"),
    [
        (None, None),
        (None, Close(1011, "keepalive ping timeout")),
        (Close(1011, ""), None),
    ],
)
async def test_transient_connection_close_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    received: Close | None,
    sent: Close | None,
) -> None:
    stream = AsyncMock(side_effect=[ConnectionClosedError(received, sent), True])
    sleep = AsyncMock()
    monkeypatch.setattr(deepseek, "stream_session", stream)
    monkeypatch.setattr(deepseek.asyncio, "sleep", sleep)

    await deepseek.run_tail(
        "ws://octomate.example/stream",
        SESSION_ID,
        tmp_path / "session",
        str(tmp_path),
        "test-token",
        DSH_URL,
    )

    assert stream.await_count == 2
    sleep.assert_awaited_once_with(2.0)


def test_new_entries_pages_back_to_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = PagingGateway([*turn(0), *turn(4)], page_size=3)

    entries = new_entries(gateway, SESSION_ID, 2)

    assert [seq_of(item) for item in entries] == [2, 3, 4, 5, 6, 7]
    # Two windows of three walked the tail back to the cursor, no further.
    assert len(gateway.calls) == 2


def test_new_entries_from_zero_takes_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = PagingGateway(turn(0), page_size=100)

    entries = new_entries(gateway, SESSION_ID, 0)

    assert [seq_of(item) for item in entries] == [0, 1, 2, 3]
    assert len(gateway.calls) == 1


def test_a_gateway_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DshHistoryClient(DSH_URL)
    monkeypatch.setattr(client, "snapshot", lambda session_id: {"type": "error"})
    with pytest.raises(DshCompatibilityError, match="no cursor"):
        new_entries(client, SESSION_ID, 0)


def test_a_completed_tail_ships_whole() -> None:
    entries = turn(0)
    assert shippable(entries) == entries


def test_an_interrupted_tail_withholds_the_trailing_turn() -> None:
    """The interrupted close may be the reader's synthesis for a turn another
    process is still writing; shipping it would advance the cursor past seqs
    whose truth is not yet written."""
    entries = [*turn(0), *turn(4, reason="interrupted")]

    shipped = shippable(entries)

    # The first turn ships whole; the trailing turn — prompt included — waits.
    assert [seq_of(item) for item in shipped] == [0, 1, 2, 3]


def test_an_interrupted_turn_with_a_successor_ships() -> None:
    entries = [
        *turn(0, reason="interrupted"),
        entry(4, "session/title", {"title": "t"}),
    ]
    assert shippable(entries) == entries


def test_session_origin_reads_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    def listing(method: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        assert method == "session/list"
        assert payload == {"_request": {}}
        return {
            "items": [
                {"sessionId": "session-parent"},
                {"sessionId": SESSION_ID, "origin": "subagent"},
            ]
        }

    client = DshHistoryClient(DSH_URL)
    monkeypatch.setattr(client, "rpc", listing)
    assert session_origin(client, SESSION_ID) == "subagent"
    assert session_origin(client, "session-parent") is None
