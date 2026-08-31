"""`octomate deepseek tail` — the client half of the dsh event stream.

Runs on the machine a native dsh session lives on, but unlike the Claude and
Codex tails it opens no file: dsh's session log is zstd-framed and only
advances at checkpoints, while the harness's own `/api` gateway serves it
decoded and unpacked (`session.history`) — cold sessions included, other dsh
processes under the same `$DSH_HOME` included. So this client reads the local
gateway and ships each history entry as one framed line to Octomate's stream
endpoint, the event's dense `seq` standing where a file tail's byte offsets
stand (`start = seq`, `end = seq + 1`). The server assembles the turns; the
server never speaks to this machine's dsh.

One safety rule is the only interpretation this client performs beyond
framing: for a session whose last turn is still open, the gateway's reader
*synthesizes* `turn/end {reason: interrupted}` closers in memory — and if the
session's own process later completes the turn, the log's real events land on
those same seqs with different content. Shipping the synthesis would advance
the cursor past seqs whose truth is not yet written. So a fetch whose final
event is an interrupted `turn/end` withholds everything from that turn's
`turn/start` on; the next poll re-reads, and the turn ships once anything
follows it — a successor event is the proof the closers are the log's own.

Spawned per session by the launcher hook (`launch.py`), detached; one
instance per session via the same flock the file tails use. The server owns
the cursor: each connect re-asks where to resume (the committed floor), so
this process holds no durable state. It ends on the server's `finalize` (a
`Stop` settled), on the idle window, or on a policy refusal (close 1008 — a
driven session, or a stale protocol).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from time import monotonic
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from octomate_cli.config import SECRET_ENV, resolved_secret
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
from octomate_cli.tail import (
    BACKOFF_CAP,
    CLIENT_VERSION,
    IDLE_TIMEOUT,
    LOCK_GRACE,
    LOCK_POLL,
    REFUSED,
)

# The gateway is polled rather than watched: an attached session's history is
# served live from memory, a cold one's advances at checkpoint flushes, and
# both are cheap local RPCs.
POLL_INTERVAL = 1.0

RPC_TIMEOUT = 10

# One `session.history` page, counted in whole messages (the RPC's own unit).
PAGE_MESSAGES = 200


def rpc(dsh_url: str, method: str, payload: dict[str, object]) -> object | None:
    """One unary gateway call; the ok-branch value, or None on any failure —
    the poll loop retries, and a dsh that went away ends the tail through the
    idle window rather than a traceback."""
    body = {
        "type": "client-request",
        "rpcId": str(uuid4()),
        "method": method,
        "payload": payload,
    }
    request = urllib.request.Request(
        f"{dsh_url.rstrip('/')}/api/{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=RPC_TIMEOUT) as response:
            answer = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    result = answer.get("result") if isinstance(answer, dict) else None
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    return result.get("value")


def event_of(entry: object) -> dict[str, object] | None:
    if not isinstance(entry, dict):
        return None
    event = entry.get("event")
    return event if isinstance(event, dict) else None


def seq_of(entry: object) -> int | None:
    event = event_of(entry)
    if event is None:
        return None
    seq = event.get("seq")
    return seq if isinstance(seq, int) else None


def new_entries(dsh_url: str, session_id: str, cursor: int) -> list[dict[str, object]]:
    """The session's history entries at or past `cursor`, in seq order — pages
    walked back from the tail until the window reaches the cursor."""
    collected: dict[int, dict[str, object]] = {}
    before: int | None = None
    while True:
        payload: dict[str, object] = {
            "sessionId": session_id,
            "maxMessages": PAGE_MESSAGES,
        }
        if before is not None:
            payload["beforeSeq"] = before
        value = rpc(dsh_url, "session.history", payload)
        if not isinstance(value, dict):
            return []
        entries = value.get("events")
        if not isinstance(entries, list):
            return []
        first_seq: int | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            seq = seq_of(entry)
            if seq is None:
                continue
            collected[seq] = entry
            first_seq = seq if first_seq is None else min(first_seq, seq)
        if not value.get("hasMore") or first_seq is None or first_seq <= cursor:
            break
        before = first_seq
    return [collected[seq] for seq in sorted(collected) if seq >= cursor]


def reason_kind(event: dict[str, object]) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if not isinstance(reason, dict):
        return None
    kind = reason.get("kind")
    return kind if isinstance(kind, str) else None


def shippable(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """The prefix safe to ship: everything, unless the fetch ends on an
    interrupted `turn/end` — possibly the reader's in-memory synthesis for a
    turn another process is still writing — in which case the whole trailing
    turn (its `turn/start` on) is withheld for a later poll to prove out."""
    if not entries:
        return entries
    last = event_of(entries[-1])
    if last is None or last.get("type") != "turn/end":
        return entries
    if reason_kind(last) != "interrupted":
        return entries
    for index in range(len(entries) - 1, -1, -1):
        event = event_of(entries[index])
        if event is not None and event.get("type") == "turn/start":
            return entries[:index]
    return []


def session_origin(dsh_url: str, session_id: str) -> str | None:
    value = rpc(dsh_url, "session.list", {})
    if not isinstance(value, dict):
        return None
    items = value.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("sessionId") == session_id:
            origin = item.get("origin")
            return origin if isinstance(origin, str) else None
    return None


async def stream_session(
    url: str,
    session_id: str,
    transcript_path: Path,
    cwd: str,
    secret: str,
    dsh_url: str,
) -> bool:
    """One connection's life. True when the session is done (the server said
    finalize, or it went idle); False to reconnect and resume."""
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
        cursor = welcome.offsets.get(SESSION_FILE, 0)

        stop = asyncio.Event()
        finalizing = False

        async def receive_server() -> None:
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

        async def pump() -> bool:
            nonlocal cursor
            entries = await asyncio.to_thread(new_entries, dsh_url, session_id, cursor)
            sent = False
            for entry in shippable(entries):
                seq = seq_of(entry)
                if seq is None or seq < cursor:
                    continue
                if seq > cursor:
                    # The gateway skipped seqs it should serve densely; a
                    # shipped gap would close the stream at 4000, so resync
                    # by reconnecting instead.
                    raise RuntimeError(
                        f"seq gap from dsh: expected {cursor}, got {seq}"
                    )
                await websocket.send(
                    StreamLine(
                        agent_id=None,
                        start=seq,
                        end=seq + 1,
                        line=json.dumps(entry, separators=(",", ":")),
                    ).model_dump_json()
                )
                cursor = seq + 1
                sent = True
            return sent

        receiver = asyncio.create_task(receive_server())
        last_active = monotonic()
        try:
            while True:
                if await pump():
                    last_active = monotonic()
                if stop.is_set():
                    break
                if monotonic() - last_active > IDLE_TIMEOUT:
                    finalizing = True
                    break
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(POLL_INTERVAL):
                        await stop.wait()
            if not finalizing:
                return False  # the socket dropped: reconnect and resume
            receiver.cancel()
            await pump()  # final drain past the finalize
            await websocket.send(StreamEof().model_dump_json())
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                async with asyncio.timeout(10):
                    await websocket.recv()
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
    secret: str,
    dsh_url: str,
) -> None:
    """The reconnect loop around `stream_session`, mirroring the file tails':
    resume after drops, back off while the server is unreachable, stop once
    the session goes quiet — the next prompt's launcher starts a fresh tail."""
    attempt = 0
    while True:
        try:
            if await stream_session(
                url, session_id, transcript_path, cwd, secret, dsh_url
            ):
                return
            attempt = 0
        except ConnectionClosed as closed:
            code = closed.rcvd.code if closed.rcvd is not None else None
            if code == REFUSED:
                reason = closed.rcvd.reason if closed.rcvd is not None else ""
                print(f"octomate: stream refused: {reason}", file=sys.stderr)
                return
            attempt += 1
        except InvalidStatus as denied:
            if denied.response.status_code in {401, 403}:
                print(
                    "octomate: stream denied — the credential does not match "
                    "Octomate's secret.",
                    file=sys.stderr,
                )
                return
            attempt += 1
        except (OSError, InvalidHandshake, TimeoutError, RuntimeError):
            attempt += 1
        await asyncio.sleep(min(BACKOFF_CAP, float(2**attempt)))


def main(
    *,
    session_id: str,
    transcript_path: Path,
    url: str,
    cwd: str,
    dsh_url: str,
) -> None:
    secret = resolved_secret()
    if not secret:
        print(
            f"octomate: no credential — {SECRET_ENV} is unset and the "
            "client config holds none, so this session is not being streamed. "
            "Run `octomate configure`.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # A dsh subagent child is its parent's story; the server never sees the
    # session header, so the classification happens here, against the same
    # gateway the events come from.
    if session_origin(dsh_url, session_id) == "subagent":
        return
    lock_path = Path(tempfile.gettempdir()) / f"octomate-tail-{session_id}.lock"
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
        asyncio.run(run_tail(url, session_id, transcript_path, cwd, secret, dsh_url))
