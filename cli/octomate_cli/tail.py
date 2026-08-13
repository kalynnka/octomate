"""`octomate claude tail` — the client half of the transcript stream.

Runs on the machine a native session lives on: watches its transcript (and the
subagent files beside it), frames complete lines, and ships them raw to Octomate's
stream endpoint. No parsing happens here — the server's tailer assembles the turns —
so a transcript format change never needs this client updated.

Spawned per session by the launcher hook (`launch.py`), detached so the turn never
waits on it; one instance per session, enforced with a file lock the OS releases with
the process. The server owns every cursor: each connect re-asks where to resume, so
this process holds no durable state and a crash anywhere costs a re-stream, never a
loss. It ends when the server says `finalize` (the session ended), when the session
goes quiet past the idle window (drain and `eof` — the same bet the server tailer's
idle reclaim makes), or when the server refuses it outright (close code 1008).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import monotonic

from watchfiles import awatch
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from octomate_cli.hooks import HOOK_SECRET_ENV
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

# Mirror the server tailer's cadence: the watch wakes at least this often, and a
# session quiet this long is drained and closed out.
IDLE_TIMEOUT = 30 * 60.0
IDLE_POLL_MS = 60_000

# Reconnect backoff, capped so a long server outage retries steadily rather than
# giving up — the retries stop on their own once the session goes idle-quiet.
BACKOFF_CAP = 60.0

# A policy refusal (bad credential, stale protocol, a session the server drives or
# already tails locally) does not heal by retrying; 4000 is the server's resync close
# (an offset gap), which a fresh connect heals.
REFUSED = 1008

try:
    CLIENT_VERSION = version("octomate-cli")
except PackageNotFoundError:
    # Running from an uninstalled source tree; version-less beats not streaming.
    CLIENT_VERSION = "unknown"


@dataclass
class FileCursor:
    """One file's read state. Complete lines only leave through `read_lines`; a
    trailing fragment stays unread and is re-read whole next time, so a line split
    across two reads is never half-shipped."""

    path: Path
    offset: int = 0

    def read_lines(self) -> list[tuple[int, int, bytes]]:
        """New complete lines as (start, end, raw) — offsets in this file's own byte
        space, `end` past the newline each line was framed on."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:  # truncation guard, mirroring the server pump's
            self.offset = 0
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
        lines: list[tuple[int, int, bytes]] = []
        for raw in chunk.split(b"\n")[:-1]:
            start = self.offset
            self.offset += len(raw) + 1
            # Blank lines ship too: the server advances its expected offset from
            # every frame, so skipping one here would read as a gap there.
            lines.append((start, self.offset, raw))
        return lines


@dataclass
class SessionTail:
    """One session's cursors: the transcript's own, and one per subagent file as they
    appear under `<session-id>/subagents/`, each seeded from the offset the server's
    welcome named for it."""

    session_id: str
    transcript_path: Path
    offsets: dict[str, int] = field(default_factory=dict)
    files: dict[str, FileCursor] = field(default_factory=dict)
    last_active: float = field(default_factory=monotonic)

    @property
    def subagents_dir(self) -> Path:
        return self.transcript_path.with_suffix("") / "subagents"

    def discover(self) -> list[str]:
        """The file keys present on disk right now: the session itself, plus every
        subagent file — new children join the pump the moment they exist."""
        keys = [SESSION_FILE]
        if self.subagents_dir.is_dir():
            keys += sorted(
                path.stem.removeprefix("agent-")
                for path in self.subagents_dir.glob("agent-*.jsonl")
            )
        return keys

    def cursor(self, key: str) -> FileCursor:
        cursor = self.files.get(key)
        if cursor is None:
            path = (
                self.transcript_path
                if key == SESSION_FILE
                else self.subagents_dir / f"agent-{key}.jsonl"
            )
            cursor = FileCursor(path, self.offsets.get(key, 0))
            self.files[key] = cursor
        return cursor

    async def pump(self, websocket: ClientConnection) -> bool:
        """Ship every complete new line of every file; True if anything went out."""
        sent = False
        for key in self.discover():
            cursor = self.cursor(key)
            for start, end, raw in cursor.read_lines():
                # Transcripts are UTF-8; a mangled byte is replaced rather than
                # allowed to wedge this tail forever on one line — the server skips
                # what it cannot parse, which is the same posture.
                await websocket.send(
                    StreamLine(
                        agent_id=key or None,
                        start=start,
                        end=end,
                        line=raw.decode("utf-8", errors="replace"),
                    ).model_dump_json()
                )
                sent = True
        if sent:
            self.last_active = monotonic()
        return sent


async def stream_session(
    url: str, session_id: str, transcript_path: Path, cwd: str, secret: str
) -> bool:
    """One connection's life. True when the session is done (the server said finalize,
    or it went idle and drained out); False to reconnect and resume."""
    async with connect(
        url, additional_headers={"Authorization": f"Bearer {secret}"}
    ) as websocket:
        await websocket.send(
            StreamHello(
                protocol=STREAM_PROTOCOL,
                session_id=session_id,
                transcript_path=str(transcript_path),
                cwd=cwd,
                client_version=CLIENT_VERSION,
            ).model_dump_json()
        )
        welcome = server_message_adapter.validate_json(await websocket.recv())
        if not isinstance(welcome, StreamWelcome):
            raise RuntimeError(f"expected a welcome message, got {welcome.type!r}")
        tail = SessionTail(session_id, transcript_path, offsets=dict(welcome.offsets))

        stop = asyncio.Event()
        finalizing = False

        async def receive_server() -> None:
            # The only message after welcome is finalize; a dropped socket ends the
            # watch too, and the outer loop reconnects.
            nonlocal finalizing
            while True:
                try:
                    raw = await websocket.recv()
                except ConnectionClosed:
                    stop.set()
                    return
                if isinstance(
                    server_message_adapter.validate_json(raw), StreamFinalize
                ):
                    finalizing = True
                    stop.set()
                    return

        receiver = asyncio.create_task(receive_server())
        try:
            await tail.pump(websocket)
            async for _ in awatch(
                transcript_path.parent,
                stop_event=stop,
                # Subagent files live under `<session-id>/subagents/`, a level down.
                recursive=True,
                rust_timeout=IDLE_POLL_MS,
                yield_on_timeout=True,
            ):
                await tail.pump(websocket)
                if monotonic() - tail.last_active > IDLE_TIMEOUT:
                    finalizing = True
                    break
            if not finalizing:
                return False  # the socket dropped mid-watch: reconnect and resume
            receiver.cancel()
            await tail.pump(websocket)  # final drain to EOF
            await websocket.send(StreamEof().model_dump_json())
            # Wait out the server's close so the eof is consumed, bounded so a
            # wedged server cannot park this process forever.
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                async with asyncio.timeout(10):
                    await websocket.recv()
            return True
        finally:
            receiver.cancel()
            with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
                await receiver


async def run_tail(
    url: str, session_id: str, transcript_path: Path, cwd: str, secret: str
) -> None:
    """The reconnect loop around `stream_session`: resume after drops, back off while
    the server is unreachable, and stop retrying once the session itself has gone
    quiet — a fresh session's launcher starts a fresh tail."""
    attempt = 0
    while True:
        try:
            if await stream_session(url, session_id, transcript_path, cwd, secret):
                return
            attempt = 0  # the connection worked; the drop was the network's
        except ConnectionClosed as closed:
            code = closed.rcvd.code if closed.rcvd is not None else None
            if code == REFUSED:
                reason = closed.rcvd.reason if closed.rcvd is not None else ""
                print(f"octomate: stream refused: {reason}", file=sys.stderr)
                return
            attempt += 1
        except InvalidStatus as denied:
            # A pre-accept denial arrives as an HTTP handshake rejection, not a
            # close code — `hook_guard`'s 401 — and does not heal by retrying.
            if denied.response.status_code in {401, 403}:
                print(
                    "octomate: stream denied — the hook credential does not match "
                    "Octomate's hook_secret.",
                    file=sys.stderr,
                )
                return
            attempt += 1
        except (OSError, InvalidHandshake, TimeoutError):
            attempt += 1
        try:
            quiet = time.time() - transcript_path.stat().st_mtime
        except FileNotFoundError:
            return  # the transcript is gone: nothing left to ship
        if quiet > IDLE_TIMEOUT:
            return
        await asyncio.sleep(min(BACKOFF_CAP, float(2**attempt)))


def main(*, session_id: str, transcript_path: Path, url: str, cwd: str) -> None:
    secret = os.environ.get(HOOK_SECRET_ENV)
    if not secret:
        print(
            f"octomate: {HOOK_SECRET_ENV} is unset — this session is not being "
            "streamed. Export it to match Octomate's hook_secret.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # One tail per session: the launcher fires on every prompt, and the lock is what
    # makes the second spawn a no-op. flock dies with the process, so there is no
    # stale-pidfile state to manage.
    lock_path = Path(tempfile.gettempdir()) / f"octomate-tail-{session_id}.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        asyncio.run(run_tail(url, session_id, transcript_path, cwd, secret))
