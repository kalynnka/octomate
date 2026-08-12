"""The transcript stream's wire protocol — what a client-side tail exchanges with the
server's stream endpoint when a native session's transcript lives on another machine.

The payload is raw transcript lines plus the byte offsets they occupy in the client's
own file. The client never parses what it ships — every translation stays server-side,
so a transcript format change is a server-only fix — which also keeps this protocol
agent-neutral: the same frames carry a Claude session file or, later, a Codex rollout,
and which tree a stream feeds is the mounting endpoint's business.

The server is the only cursor authority. A connect opens with `hello`/`welcome`: the
client claims a session, the server answers where each of its files resumes (recomputed
from the committed runs), and the client holds no durable state — a crash anywhere
costs a re-stream, never a loss. Lines then flow contiguously per file; `finalize`
(server → client, relaying `SessionEnd` from the hook pipe) and `eof` (client → server,
"drained, commit the trailing turns") close a session out.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

# Bumped when a message's meaning changes. The server refuses a mismatch at hello and
# names both versions, so a stale client surfaces as one clear line instead of being
# half-understood; the session still degrades to hooks-only ingest.
STREAM_PROTOCOL = 1

# The key the session transcript itself streams under in per-file maps; a subagent's
# file streams under its agent id.
SESSION_FILE = ""


class StreamHello(BaseModel):
    """The client's opening claim: which session it tails, where the transcript lives
    on the client's machine (a label in the *client's* namespace — the server never
    opens it), and the directory the session runs in, which files the session's thread
    under its project exactly as the http hooks' `cwd` does."""

    type: Literal["hello"] = "hello"
    protocol: int
    session_id: str
    transcript_path: str
    cwd: str = ""
    # The shipping client's own version (the octomate package), logged at attach for
    # skew diagnosis. The Claude Code version is not carried here: it rides every
    # transcript line, which the server parses anyway.
    client_version: str = ""


class StreamLine(BaseModel):
    """One complete transcript line and the byte range it occupies in its file.

    `start`/`end` are the client file's own offsets, `end` past the newline the line
    was framed on — the same range the assembled run's `start_offset`/`end_offset`
    records. Lines arrive contiguously per file, blank ones included (skipping one
    would read as a gap); the server closes on a gap and the reconnect re-asks where
    to resume.
    """

    type: Literal["line"] = "line"
    # None: the session transcript; else the subagent whose file this line is from.
    agent_id: str | None = None
    start: int
    end: int
    line: str


class StreamEof(BaseModel):
    """The client drained its files to EOF and is done — answering a `finalize`, or on
    its own idle give-up. The server commits the trailing turns on this; a connection
    that drops without it leaves them uncommitted for the next connect to re-stream."""

    type: Literal["eof"] = "eof"


class StreamWelcome(BaseModel):
    """The server's answer to hello: where each file resumes, keyed by `SESSION_FILE`
    or agent id. A file absent from the map starts at 0."""

    type: Literal["welcome"] = "welcome"
    offsets: dict[str, int]


class StreamFinalize(BaseModel):
    """Server → client: the session ended (`SessionEnd` arrived on the hook pipe) —
    drain to EOF, answer with `eof`, and exit."""

    type: Literal["finalize"] = "finalize"


StreamClientMessage = Annotated[
    StreamHello | StreamLine | StreamEof, Field(discriminator="type")
]
StreamServerMessage = Annotated[
    StreamWelcome | StreamFinalize, Field(discriminator="type")
]

client_message_adapter: TypeAdapter[StreamClientMessage] = TypeAdapter(
    StreamClientMessage
)
server_message_adapter: TypeAdapter[StreamServerMessage] = TypeAdapter(
    StreamServerMessage
)
