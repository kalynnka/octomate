"""`octomate claude tail` / `octomate codex tail` — the client half of the transcript
stream.

Runs on the machine a native session lives on: watches its transcript (and the
subagent files beside it), frames complete lines, and ships them raw to Octomate's
stream endpoint. The server assembles the turns. Agent-specific tails can extend
line framing while sharing the file and connection lifecycle.

Subagent file discovery differs between agents. Claude keeps them under
`<session-id>/subagents/`, discoverable by glob and keyed by agent id. Codex writes a
child rollout as a sibling file only its content identifies, so the launcher hook
spools the exact path each `SubagentStop` names into a per-session file here, and the
tail registers each spooled basename and opening `session_meta` with the server.
All transcript lines use the conversation UUID returned by the server.

Spawned per session by the launcher hook (`launch.py`), detached so the turn never
waits on it; one instance per config scope, agent and session, enforced with a file
lock the OS releases with the process. The server owns every cursor: each connect
re-asks where to resume, so this process holds no durable state and a crash anywhere
costs a re-stream, never a loss. It ends when the server says `finalize` (the turn
stopped or the session ended; the next prompt's launcher spawns a fresh tail), when
the session goes quiet past the idle window (drain and `eof` — the same bet the server
tailer's idle reclaim makes), or when the server refuses it outright (close code 1008).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from itertools import zip_longest
from pathlib import Path
from time import monotonic
from typing import Literal

from octomate_protocol.stream import (
    STREAM_PROTOCOL,
    StreamEof,
    StreamFinalize,
    StreamHello,
    StreamLine,
    StreamOpenSubagent,
    StreamSnapshotStart,
    StreamSnapshotStored,
    StreamSubagentOpened,
    StreamWelcome,
    server_message_adapter,
)
from pydantic import UUID7
from uuid_utils.compat import uuid7
from watchfiles import awatch
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from octomate_cli.config import (
    CLISettings,
    cli_settings,
    project_config_path,
    user_config_path,
)

# Native notifications can miss appends to an open file. Re-read at least once a
# second; the idle lifetime is independent of this live-delivery cadence.
IDLE_TIMEOUT = 5 * 60.0
IDLE_POLL_MS = 1_000

# Reconnect backoff, capped so a long server outage retries steadily rather than
# giving up — the retries stop on their own once the session goes idle-quiet.
BACKOFF_CAP = 60.0

# A policy refusal (bad credential, stale protocol, a session the server drives or
# already tails locally) does not heal by retrying; 4000 is the server's resync close
# (an offset gap), which a fresh connect heals.
REFUSED = 1008

# A spawn can land while the previous turn's tail is still draining out — the server
# relays `finalize` at `Stop`, and a queued prompt fires the launcher before the old
# process has exited. Wait this long for the session lock before ceding the round; a
# holder that keeps it past the window is a genuinely running tail.
LOCK_GRACE = 15.0
LOCK_POLL = 0.2

SNAPSHOT_CHUNK_SIZE = 64 * 1024
STREAM_ACK_TIMEOUT = 60.0

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

    @staticmethod
    def opening_line(path: Path) -> str | None:
        try:
            with path.open("rb") as handle:
                line = handle.readline()
        except FileNotFoundError:
            return None
        return (
            line[:-1].decode("utf-8", errors="replace")
            if line.endswith(b"\n")
            else None
        )

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


def tail_path(agent: Literal["claude", "codex", "deepseek"], session_id: str) -> Path:
    """Lock/spool basename scoped by config location, never its URL or contents."""
    config_path = project_config_path()
    if not config_path.is_file():
        config_path = user_config_path()
    key = json.dumps((str(config_path), agent, session_id)).encode()
    return (
        Path(tempfile.gettempdir()) / f"octomate-tail-{hashlib.sha256(key).hexdigest()}"
    )


@dataclass
class SessionTail:
    """Tail each transcript from the offset core gives for its conversation UUID.

    Claude discovers children in the session's subagents directory. Codex discovers
    sibling rollouts through its spool and registers their opening metadata.
    """

    session_id: str
    transcript_path: Path
    conversation_id: UUID7
    offsets: dict[UUID7, int]
    subagents: dict[str, UUID7] = field(default_factory=dict)
    files: dict[UUID7, FileCursor] = field(default_factory=dict, init=False)
    ignored: set[str] = field(default_factory=set, init=False)
    last_active: float = field(default_factory=monotonic)
    spool: Path | None = None
    stored_offset: int | None = None  # None disables transcript uploads.
    receipts: asyncio.Queue[
        StreamSubagentOpened | StreamSnapshotStored | ConnectionClosed | ValueError
    ] = field(default_factory=asyncio.Queue)
    spooled: dict[str, Path] = field(default_factory=dict)
    cwd: str = ""  # Working directory reported by the native session.

    def __post_init__(self) -> None:
        self.files[self.conversation_id] = FileCursor(
            self.transcript_path, self.offsets[self.conversation_id]
        )

    def discover(self) -> dict[str, Path]:
        """Find child files by their harness name on this machine."""
        if self.spool is not None:
            try:
                lines = self.spool.read_text().splitlines()
            except OSError:
                lines = []
            for entry in lines:
                if entry:
                    path = Path(entry)
                    self.spooled[path.name] = path
            return self.spooled
        children = self.transcript_path.with_suffix("") / "subagents"
        return {
            path.stem.removeprefix("agent-"): path
            for path in sorted(children.glob("agent-*.jsonl"))
        }

    async def open_subagent(
        self, websocket: ClientConnection, name: str, path: Path
    ) -> None:
        """Bind a discovered file to its durable conversation before reading from its offset."""
        if name in self.ignored:
            return
        id_ = self.subagents.get(name)
        if id_ is None:
            first_line = None
            if self.spool is not None:
                if not self.files[self.conversation_id].offset:
                    return
                first_line = await asyncio.to_thread(FileCursor.opening_line, path)
                if first_line is None:
                    return
            await websocket.send(
                StreamOpenSubagent(name=name, first_line=first_line).model_dump_json()
            )
            async with asyncio.timeout(STREAM_ACK_TIMEOUT):
                opened = await self.receipts.get()
            if isinstance(opened, (ConnectionClosed, ValueError)):
                raise opened
            if not isinstance(opened, StreamSubagentOpened):
                raise ValueError("Expected a subagent registration")
            if opened.conversation_id is None:
                self.ignored.add(name)
                return
            id_ = opened.conversation_id
            self.subagents[name] = id_
            self.offsets[id_] = opened.offset
        if id_ not in self.files:
            self.files[id_] = FileCursor(path, self.offsets[id_])

    async def pump(self, websocket: ClientConnection) -> bool:
        """Send complete lines under their conversation UUID, interleaving files."""
        root = self.conversation_id
        root_lines = iter(self.files[root].read_lines())
        first = next(root_lines, None)
        sent = first is not None
        if first is not None:
            # Codex identifies children from the parent rollout's opening metadata.
            await websocket.send((await self.frame(root, *first)).model_dump_json())
        for name, path in self.discover().items():
            await self.open_subagent(websocket, name, path)
        children = [id_ for id_ in self.files if id_ != root]
        keys = [*children, root]
        streams = [iter(self.files[id_].read_lines()) for id_ in children]
        streams.append(root_lines)
        for batch in zip_longest(*streams):
            for id_, line in zip(keys, batch, strict=True):
                if line is None:
                    continue
                start, end, raw = line
                frame = await self.frame(id_, start, end, raw)
                await websocket.send(frame.model_dump_json())
                sent = True
        if sent:
            self.last_active = monotonic()
        if self.stored_offset is not None:
            await self.upload_snapshot(websocket, start=self.stored_offset)
        return sent

    async def frame(
        self, conversation_id: UUID7, start: int, end: int, raw: bytes
    ) -> StreamLine:
        """Frame an unchanged transcript line; agent tails may attach local content."""
        return StreamLine(
            conversation_id=conversation_id,
            start=start,
            end=end,
            line=raw.decode("utf-8", errors="replace"),
        )

    def snapshot(self, end: int, *, start: int = 0) -> Iterator[bytes]:
        """Read an already streamed range without resending the stored prefix."""
        if start < 0 or end <= start or end > self.files[self.conversation_id].offset:
            raise ValueError("Snapshot boundary must be within the streamed transcript")
        with self.transcript_path.open("rb") as handle:
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    raise ValueError(
                        "Snapshot boundary must start after a complete line"
                    )
            handle.seek(end - 1)
            if handle.read(1) != b"\n":
                raise ValueError(
                    "Snapshot boundary must end a complete transcript line"
                )
            handle.seek(start)
            remaining = end - start
            while remaining:
                chunk = handle.read(min(remaining, SNAPSHOT_CHUNK_SIZE))
                if not chunk:
                    raise ValueError("Transcript was truncated during snapshot upload")
                remaining -= len(chunk)
                yield chunk

    async def upload_snapshot(self, websocket: ClientConnection, *, start: int) -> None:
        """Upload only new transcript bytes and require their acknowledgment."""
        end = self.files[self.conversation_id].offset
        if start == end:
            return
        if start > end:
            raise ValueError("Transcript was truncated since its last upload")
        request = StreamSnapshotStart(
            conversation_id=self.conversation_id,
            transfer_id=uuid7(),
            start=start,
            end=end,
        )
        await websocket.send(request.model_dump_json())
        for chunk in self.snapshot(request.end, start=request.start):
            await websocket.send(chunk)
        async with asyncio.timeout(STREAM_ACK_TIMEOUT):
            stored = await self.receipts.get()
        if isinstance(stored, (ConnectionClosed, ValueError)):
            raise stored
        if (
            not isinstance(stored, StreamSnapshotStored)
            or stored.transfer_id != request.transfer_id
        ):
            raise ValueError("Expected acknowledgment for the requested snapshot")
        self.stored_offset = end


async def stream_session(
    url: str,
    session_id: str,
    transcript_path: Path,
    cwd: str,
    token: str,
    spool: Path | None = None,
    *,
    tail_type: type[SessionTail] = SessionTail,
) -> bool:
    """One connection's life. True when the session is done (the server said finalize,
    or it went idle and drained out); False to reconnect and resume."""
    async with connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}
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
        tail = tail_type(
            session_id,
            transcript_path,
            conversation_id=welcome.conversation_id,
            offsets=dict(welcome.offsets),
            subagents={name: id_ for id_, name in welcome.subagents.items()},
            spool=spool,
            cwd=cwd,
        )
        checkpoint = welcome.transcript
        if checkpoint is not None:
            digest = hashlib.sha256()
            if checkpoint.offset:
                for chunk in tail.snapshot(checkpoint.offset):
                    digest.update(chunk)
            if digest.hexdigest() != checkpoint.sha256:
                raise ValueError("Transcript prefix differs from the stored snapshot")
            tail.stored_offset = checkpoint.offset

        stop = asyncio.Event()
        finalized = asyncio.Event()
        idle = False

        async def receive_server() -> None:
            try:
                while True:
                    message = server_message_adapter.validate_json(
                        await websocket.recv()
                    )
                    if isinstance(message, StreamFinalize):
                        finalized.set()
                        stop.set()
                    elif isinstance(
                        message, (StreamSubagentOpened, StreamSnapshotStored)
                    ):
                        tail.receipts.put_nowait(message)
                    else:
                        raise ValueError(f"Unexpected server message: {message.type}")
            except (ConnectionClosed, ValueError) as error:
                tail.receipts.put_nowait(error)
            finally:
                stop.set()

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
                    idle = True
                    break
            if not finalized.is_set() and not idle:
                return False  # the socket dropped mid-watch: reconnect and resume
            await tail.pump(websocket)  # final drain to EOF
            await websocket.send(StreamEof().model_dump_json())
            # Wait out the server's close so the eof is consumed, bounded so a
            # wedged server cannot park this process forever.
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                async with asyncio.timeout(10):
                    await receiver
            return True
        finally:
            receiver.cancel()
            with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
                await receiver


async def run_tail(
    url: str,
    session_id: str,
    transcript_path: Path,
    cwd: str,
    token: str,
    spool: Path | None = None,
    *,
    tail_type: type[SessionTail] = SessionTail,
) -> None:
    """The reconnect loop around `stream_session`: resume after drops, back off while
    the server is unreachable, and stop retrying once the session itself has gone
    quiet — a fresh session's launcher starts a fresh tail."""
    attempt = 0
    while True:
        try:
            if await stream_session(
                url,
                session_id,
                transcript_path,
                cwd,
                token,
                spool,
                tail_type=tail_type,
            ):
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
                    "octomate: stream denied — a valid API token with hooks scope "
                    "is required. Run `octomate configure --token <api-token>`.",
                    file=sys.stderr,
                )
                return
            attempt += 1
        except (OSError, InvalidHandshake, TimeoutError):
            attempt += 1
        try:
            # This single metadata probe stays synchronous in reconnect bookkeeping.
            quiet = time.time() - transcript_path.stat().st_mtime  # noqa: ASYNC240
        except FileNotFoundError:
            return  # the transcript is gone: nothing left to ship
        if quiet > IDLE_TIMEOUT:
            return
        await asyncio.sleep(min(BACKOFF_CAP, float(2**attempt)))


def main(
    *,
    agent: Literal["claude", "codex"],
    session_id: str,
    transcript_path: Path,
    url: str,
    cwd: str,
    agent_path: Path | None = None,
    tail_type: type[SessionTail] = SessionTail,
) -> None:
    runtime_path = tail_path(agent, session_id)
    spool = runtime_path.with_suffix(".paths") if agent == "codex" else None
    # Spool before the lock: when a tail already holds the session, this invocation's
    # whole job is handing it the child's path — the holder re-reads the spool on
    # every pump. O_APPEND keeps concurrent hook fires from clobbering each other.
    if spool is not None and agent_path is not None:
        with spool.open("a") as handle:
            handle.write(f"{agent_path}\n")
    token = cli_settings().token
    if not token:
        print(
            f"octomate: no credential — {CLISettings.env('token')} is unset and the "
            "client config holds none, so this session is not being streamed. "
            "Run `octomate configure --token <api-token>`.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # One tail per session in this config scope; changing TOML contents does not
    # replace its lock or affect the holder. flock dies with the process.
    lock_path = runtime_path.with_suffix(".lock")
    with lock_path.open("w") as lock:
        deadline = monotonic() + LOCK_GRACE
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    return
                time.sleep(LOCK_POLL)
        asyncio.run(
            run_tail(
                url,
                session_id,
                transcript_path,
                cwd,
                token,
                spool,
                tail_type=tail_type,
            )
        )
