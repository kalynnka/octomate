"""A native tail uploads its drained transcript and waits for its receipt."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Literal

import octomate_cli.streaming.files as tail_mod
import pytest
from octomate_cli.streaming.files import (
    SNAPSHOT_CHUNK_SIZE,
    SessionTail,
    stream_session,
)
from octomate_protocol.stream import (
    STREAM_PROTOCOL,
    StreamEof,
    StreamFinalize,
    StreamHello,
    StreamLine,
    StreamSnapshotCursor,
    StreamSnapshotStart,
    StreamSnapshotStored,
    StreamWelcome,
    client_message_adapter,
)
from uuid_utils.compat import uuid7
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed


def test_snapshot_preserves_raw_bytes_and_excludes_later_lines(tmp_path: Path) -> None:
    prefix = b'{"text":"' + b"x" * SNAPSHOT_CHUNK_SIZE + b'\xff"}\r\n'
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(prefix + b'{"next_turn":true}\n')
    tail = SessionTail("source", transcript, offsets={"": len(prefix)})

    chunks = list(tail.snapshot(len(prefix)))

    assert b"".join(chunks) == prefix
    assert len(chunks) == 2
    assert max(map(len, chunks)) <= SNAPSHOT_CHUNK_SIZE
    assert tail.cursor("").offset == len(prefix)


@pytest.mark.parametrize("end", [0, -1, 3, 10])
def test_snapshot_refuses_invalid_boundaries(tmp_path: Path, end: int) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(b"one\ntwo\n")
    tail = SessionTail("source", transcript, offsets={"": 8})

    with pytest.raises(ValueError, match="Snapshot boundary"):
        list(tail.snapshot(end))


def test_snapshot_refuses_a_truncated_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(b"one\n")
    tail = SessionTail("source", transcript, offsets={"": 8})

    with pytest.raises(ValueError, match="complete transcript line"):
        list(tail.snapshot(8))


def test_incremental_snapshot_preserves_raw_suffix(tmp_path: Path) -> None:
    prefix = b"previous\n"
    suffix = b"x" * SNAPSHOT_CHUNK_SIZE + b"\xff\r\n"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(prefix + suffix + b"later\n")
    tail = SessionTail("source", transcript, offsets={"": len(prefix + suffix)})

    chunks = list(tail.snapshot(len(prefix + suffix), start=len(prefix)))

    assert b"".join(chunks) == suffix
    assert len(chunks) == 2


@pytest.mark.parametrize("content", [b"before\n", b"edited\n", b"short\n"])
async def test_stored_prefix_is_verified_before_sending_any_lines(
    tmp_path: Path, content: bytes
) -> None:
    prefix = b"before\n"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(content)
    finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def server(websocket: ServerConnection) -> None:
        try:
            await websocket.recv()
            await websocket.send(
                StreamWelcome(
                    offsets={"": len(prefix)},
                    transcript=StreamSnapshotCursor(
                        offset=len(prefix), sha256=hashlib.sha256(prefix).hexdigest()
                    ),
                ).model_dump_json()
            )
            if content == prefix:
                await websocket.send(StreamFinalize().model_dump_json())
                assert isinstance(
                    client_message_adapter.validate_json(await websocket.recv()),
                    StreamEof,
                )
            else:
                with pytest.raises(ConnectionClosed):
                    await websocket.recv()
            finished.set_result(None)
        except Exception as error:
            finished.set_exception(error)

    async with asyncio.timeout(5), serve(server, "127.0.0.1", 0) as host:
        port = next(iter(host.sockets)).getsockname()[1]
        if content == prefix:
            assert await stream_session(
                f"ws://127.0.0.1:{port}",
                "source",
                transcript,
                str(tmp_path),
                "test-token",
            )
        else:
            with pytest.raises(ValueError, match=r"prefix|complete transcript line"):
                await stream_session(
                    f"ws://127.0.0.1:{port}",
                    "source",
                    transcript,
                    str(tmp_path),
                    "test-token",
                )
        await finished


@pytest.mark.parametrize("upload_transcript", [False, True])
@pytest.mark.parametrize("resuming", [False, True])
async def test_finalize_waits_for_snapshot_receipt_before_eof(
    tmp_path: Path, upload_transcript: bool, resuming: bool
) -> None:
    previous = b'{"previous":true}\n' if resuming else b""
    prefix = b'{"turn":1}\n'
    later = b'{"turn":2}\n'
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(previous + prefix)
    finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def server(websocket: ServerConnection) -> None:
        try:
            hello = client_message_adapter.validate_json(await websocket.recv())
            assert isinstance(hello, StreamHello)
            assert hello.protocol == STREAM_PROTOCOL
            assert websocket.request is not None
            assert websocket.request.headers["Authorization"] == "Bearer test-token"
            await websocket.send(
                StreamWelcome(
                    offsets={"": len(previous)},
                    transcript=StreamSnapshotCursor(
                        offset=len(previous),
                        sha256=hashlib.sha256(previous).hexdigest(),
                    )
                    if upload_transcript
                    else None,
                ).model_dump_json()
            )
            line = client_message_adapter.validate_json(await websocket.recv())
            assert isinstance(line, StreamLine)
            assert line.start == len(previous)
            assert line.line == prefix.decode().rstrip("\n")
            # The final drain must include bytes appended just before finalization.
            with transcript.open("ab") as handle:
                handle.write(later)
            await websocket.send(StreamFinalize().model_dump_json())
            drained = client_message_adapter.validate_json(await websocket.recv())
            assert isinstance(drained, StreamLine)
            assert drained.line == later.decode().rstrip("\n")
            if upload_transcript:
                request = client_message_adapter.validate_json(await websocket.recv())
                assert isinstance(request, StreamSnapshotStart)
                assert request.start == len(previous)
                assert request.end == len(previous + prefix + later)
                assert await websocket.recv() == prefix + later
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(websocket.recv(), 0.05)
                await websocket.send(
                    StreamSnapshotStored(
                        transfer_id=request.transfer_id
                    ).model_dump_json()
                )
            assert isinstance(
                client_message_adapter.validate_json(await websocket.recv()), StreamEof
            )
            finished.set_result(None)
        except Exception as error:
            finished.set_exception(error)

    async with asyncio.timeout(5), serve(server, "127.0.0.1", 0) as host:
        port = next(iter(host.sockets)).getsockname()[1]
        assert await stream_session(
            f"ws://127.0.0.1:{port}/hooks/codex/stream",
            "source",
            transcript,
            str(tmp_path),
            "test-token",
        )
        await finished


@pytest.mark.parametrize("receipt", ["wrong_id", "wrong_message", "missing"])
async def test_unacknowledged_snapshot_does_not_send_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt: Literal["wrong_id", "wrong_message", "missing"],
) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_bytes(b"{}\n")
    monkeypatch.setattr(tail_mod, "SNAPSHOT_ACK_TIMEOUT", 0.1)
    finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def server(websocket: ServerConnection) -> None:
        try:
            await websocket.recv()  # hello
            await websocket.send(
                StreamWelcome(
                    offsets={},
                    transcript=StreamSnapshotCursor(
                        offset=0, sha256=hashlib.sha256(b"").hexdigest()
                    ),
                ).model_dump_json()
            )
            await websocket.recv()  # transcript line
            await websocket.send(StreamFinalize().model_dump_json())
            request = client_message_adapter.validate_json(await websocket.recv())
            assert isinstance(request, StreamSnapshotStart)
            assert request.end == 3
            assert await websocket.recv() == b"{}\n"
            if receipt == "wrong_id":
                await websocket.send(
                    StreamSnapshotStored(transfer_id=uuid7()).model_dump_json()
                )
            elif receipt == "wrong_message":
                await websocket.send(StreamFinalize().model_dump_json())
            with pytest.raises(ConnectionClosed):
                await websocket.recv()
            finished.set_result(None)
        except Exception as error:
            finished.set_exception(error)

    error = TimeoutError if receipt == "missing" else ValueError
    async with asyncio.timeout(5), serve(server, "127.0.0.1", 0) as host:
        port = next(iter(host.sockets)).getsockname()[1]
        with pytest.raises(error):
            await stream_session(
                f"ws://127.0.0.1:{port}/hooks/codex/stream",
                "source",
                transcript,
                str(tmp_path),
                "test-token",
            )
        await finished
