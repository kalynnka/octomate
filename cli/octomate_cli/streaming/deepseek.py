"""`octomate deepseek tail` — the client half of the dsh event stream.

Runs on the machine a native dsh session lives on, but unlike the Claude and
Codex tails it opens no file: dsh's session log is zstd-framed and only
advances at checkpoints, while the harness's own `/api` gateway serves it
decoded and unpacked (`session/follow` and `session/page`) — cold sessions included, other dsh
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
instance per config scope, agent and session via the same flock the file tails use.
The server owns the cursor: each connect re-asks where to resume (the committed
floor), so this process holds no durable state. It ends on the server's `finalize` (a
`Stop` settled), on the idle window, or on a policy refusal (close 1008, such
as a stale protocol).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from uuid import uuid4

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
from pydantic import JsonValue, TypeAdapter
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import connect as connect_sync

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.streaming.files import (
    BACKOFF_CAP,
    CLIENT_VERSION,
    IDLE_TIMEOUT,
    LOCK_GRACE,
    LOCK_POLL,
    REFUSED,
    tail_path,
)

# The gateway is polled rather than watched: an attached session's history is
# served live from memory, a cold one's advances at checkpoint flushes, and
# both are cheap local RPCs.
POLL_INTERVAL = 1.0

RPC_TIMEOUT = 10

# One `session/page` page, counted in whole messages (the RPC's own unit).
PAGE_MESSAGES = 200


json_object_adapter = TypeAdapter(dict[str, JsonValue])


class DshCompatibilityError(RuntimeError):
    """Authentication or Remote API mismatch requires operator action."""


class DshHistoryClient:
    """One authenticated cookie session for native history reads."""

    url: str
    cookies: CookieJar
    opener: urllib.request.OpenerDirector

    def __init__(self, launch_url: str) -> None:
        parts = urlsplit(launch_url)
        self.url = urlunsplit(parts._replace(path="", query="", fragment=""))
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        token = parse_qs(parts.query).get(
            "token", [os.environ.get("DSH_LAUNCH_TOKEN")]
        )[0]
        if token:
            query = urlencode({"token": token})
            try:
                with self.opener.open(f"{self.url}/?{query}", timeout=RPC_TIMEOUT):
                    pass
            except (urllib.error.URLError, OSError):
                raise DshCompatibilityError(
                    "Could not authenticate to dsh; check DSH_API_URL and DSH_LAUNCH_TOKEN"
                ) from None
            if not self.cookies:
                raise DshCompatibilityError(
                    "dsh did not issue an authentication cookie; check DSH_LAUNCH_TOKEN"
                )

    def rpc(self, method: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]:
        body = {
            "type": "client-request",
            "rpcId": str(uuid4()),
            "method": method,
            "payload": {"args": args},
        }
        request = urllib.request.Request(
            f"{self.url}/api/{method}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=RPC_TIMEOUT) as response:
                answer = json_object_adapter.validate_json(response.read())
        except urllib.error.HTTPError as error:
            raise DshCompatibilityError(
                f"dsh Remote API {method} returned HTTP {error.code}; set DSH_LAUNCH_TOKEN for authentication and update dsh and Octomate together"
            ) from None
        result = answer.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DshCompatibilityError(f"dsh Remote API {method} failed: {result}")
        return json_object_adapter.validate_python(result.get("value"))

    def snapshot(self, session_id: str) -> dict[str, JsonValue]:
        request = urllib.request.Request(f"{self.url}/api/remote.mux")
        self.cookies.add_cookie_header(request)
        headers = {"Cookie": request.get_header("Cookie", "")}
        parts = urlsplit(self.url)
        url = urlunsplit(
            parts._replace(
                scheme="wss" if parts.scheme == "https" else "ws",
                path="/api/remote.mux",
            )
        )
        with connect_sync(
            url, additional_headers=headers, open_timeout=RPC_TIMEOUT
        ) as socket:
            socket.send(
                json.dumps(
                    {
                        "type": "open",
                        "streamId": session_id,
                        "endpoint": "session/follow",
                        "payload": {
                            "args": {
                                "request": {
                                    "address": {
                                        "kind": "session",
                                        "sessionId": session_id,
                                    },
                                    "maxMessages": PAGE_MESSAGES,
                                }
                            }
                        },
                    }
                )
            )
            message = json_object_adapter.validate_json(
                socket.recv(timeout=RPC_TIMEOUT)
            )
            value = message.get("value")
            if (
                message.get("type") != "item"
                or not isinstance(value, dict)
                or value.get("type") != "snapshot"
            ):
                raise DshCompatibilityError(
                    "dsh session/follow did not return a snapshot; update dsh and Octomate together"
                )
            return value


def event_of(entry: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(entry, dict):
        return None
    event = entry.get("event")
    return event if isinstance(event, dict) else None


def seq_of(entry: JsonValue) -> int | None:
    event = event_of(entry)
    if event is None:
        return None
    seq = event.get("seq")
    return seq if isinstance(seq, int) else None


def new_entries(
    client: DshHistoryClient, session_id: str, cursor: int
) -> list[dict[str, JsonValue]]:
    """The session's history entries at or past `cursor`, in seq order — pages
    walked back from the tail until the window reaches the cursor."""
    collected: dict[int, dict[str, JsonValue]] = {}
    value = client.snapshot(session_id)
    through_seq = value.get("cursor")
    if not isinstance(through_seq, int):
        raise DshCompatibilityError("dsh snapshot has no cursor")
    while True:
        entries = value.get("records")
        if not isinstance(entries, list):
            raise DshCompatibilityError("dsh history has no records")
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
        value = client.rpc(
            "session/page",
            {
                "request": {
                    "address": {"kind": "session", "sessionId": session_id},
                    "throughSeq": through_seq,
                    "beforeSeq": first_seq,
                    "maxMessages": PAGE_MESSAGES,
                }
            },
        )
    return [collected[seq] for seq in sorted(collected) if seq >= cursor]


def reason_kind(event: dict[str, JsonValue]) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if not isinstance(reason, dict):
        return None
    kind = reason.get("kind")
    return kind if isinstance(kind, str) else None


def shippable(entries: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
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


def session_origin(client: DshHistoryClient, session_id: str) -> str | None:
    value = client.rpc("session/list", {"_request": {}})
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
    token: str,
    dsh_url: str,
) -> bool:
    """One connection's life. True when the session is done (the server said
    finalize, or it went idle); False to reconnect and resume."""
    history = await asyncio.to_thread(DshHistoryClient, dsh_url)
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
            entries = await asyncio.to_thread(new_entries, history, session_id, cursor)
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
    token: str,
    dsh_url: str,
) -> None:
    """The reconnect loop around `stream_session`, mirroring the file tails':
    resume after drops, back off while the server is unreachable, stop once
    the session goes quiet — the next prompt's launcher starts a fresh tail."""
    attempt = 0
    while True:
        try:
            if await stream_session(
                url, session_id, transcript_path, cwd, token, dsh_url
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
                    "octomate: stream denied — a valid API token with hooks scope "
                    "is required. Run `octomate configure --token <api-token>`.",
                    file=sys.stderr,
                )
                return
            attempt += 1
        except DshCompatibilityError as error:
            print(f"octomate: {error}", file=sys.stderr)
            return
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
    token = cli_settings().token
    if not token:
        print(
            f"octomate: no credential — {CLISettings.env('token')} is unset and the "
            "client config holds none, so this session is not being streamed. "
            "Run `octomate configure --token <api-token>`.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # A dsh subagent child is its parent's story; the server never sees the
    # session header, so the classification happens here, against the same
    # gateway the events come from.
    try:
        if session_origin(DshHistoryClient(dsh_url), session_id) == "subagent":
            return
    except DshCompatibilityError as error:
        print(f"octomate: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    lock_path = tail_path("deepseek", session_id).with_suffix(".lock")
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
        asyncio.run(run_tail(url, session_id, transcript_path, cwd, token, dsh_url))
