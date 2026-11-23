"""The transcript stream's wire protocol — what a client-side tail exchanges with the
server's stream endpoint when a native session's transcript lives on another machine.

The payload is raw transcript lines plus the byte offsets they occupy in the client's
own file. Turn assembly stays server-side. Codex clients additionally recognize
explicit visualization references to attach bounded HTML snapshots; raw transcript
lines and their offsets are unchanged. The same frames carry a Claude session file
or a Codex rollout; the mounting endpoint selects its assembler.

The server is the only cursor authority. A connect opens with `hello`/`welcome`: the
client claims a session, the server answers where each of its files resumes (recomputed
from the committed runs), and the client holds no durable state — a crash anywhere
costs a re-stream, never a loss. Lines then flow contiguously per file; `finalize`
(server → client, relaying `SessionEnd` from the hook pipe) and `eof` (client → server,
"drained, commit the trailing turns") close a session out.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    UUID7,
    BaseModel,
    Field,
    NonNegativeInt,
    TypeAdapter,
    model_validator,
)

from octomate_protocol.visualizations import HtmlSnapshot, codex_html_references

# Bumped when a message's meaning changes. The server refuses a mismatch at hello and
# names both versions, so a stale client surfaces as one clear line instead of being
# half-understood; the session still degrades to hooks-only ingest.
STREAM_PROTOCOL = 3


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
    # The shipping client's own version (the octomate-cli package), logged at attach for
    # skew diagnosis. The Claude Code version is not carried here: it rides every
    # transcript line, which the server parses anyway.
    client_version: str = ""
    catchup_end: NonNegativeInt = Field(
        default=0,
        description="Transcript byte length at connection time; earlier lines restore history.",
    )


class StreamLine(BaseModel):
    """One complete transcript line and the byte range it occupies in its file.

    `start`/`end` are the client file's own offsets, `end` past the newline the line
    was framed on — the same range the assembled run's `start_offset`/`end_offset`
    records. Lines arrive contiguously per file, blank ones included (skipping one
    would read as a gap); the server closes on a gap and the reconnect re-asks where
    to resume.
    """

    type: Literal["line"] = "line"
    conversation_id: UUID7
    start: int
    end: int
    line: str
    visualizations: list[HtmlSnapshot] = Field(
        default_factory=list,
        description="HTML snapshots explicitly referenced by this Codex assistant line, read on the client.",
    )

    @model_validator(mode="after")
    def snapshots_match_references(self) -> Self:
        if self.visualizations and (
            [snapshot.reference for snapshot in self.visualizations]
            != codex_html_references(self.line)
        ):
            raise ValueError("HTML snapshots must match the root assistant references")
        return self


class StreamEof(BaseModel):
    """The client drained its files to EOF and is done — answering a `finalize`, or on
    its own idle give-up. The server commits the trailing turns on this; a connection
    that drops without it leaves them uncommitted for the next connect to re-stream."""

    type: Literal["eof"] = "eof"


class StreamSnapshotCursor(BaseModel):
    """The durable transcript prefix the client must verify before appending."""

    offset: int = Field(ge=0, description="Number of transcript bytes already stored.")
    sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="SHA-256 of that prefix."
    )


class TranscriptOffsets(BaseModel):
    """Resume positions keyed by Octomate conversation UUID."""

    offsets: dict[UUID7, NonNegativeInt]
    subagents: dict[UUID7, str] = Field(
        default_factory=dict, description="Local child file identifiers for discovery."
    )


class StreamWelcome(TranscriptOffsets):
    """The session's conversation UUID and the resume positions for its files."""

    type: Literal["welcome"] = "welcome"
    conversation_id: UUID7
    transcript: StreamSnapshotCursor | None = Field(
        default=None,
        description="Continuously upload bytes after this verified prefix; None disables upload.",
    )


class StreamOpenSubagent(BaseModel):
    """Resolve a newly discovered child before sending its transcript."""

    type: Literal["open_subagent"] = "open_subagent"
    name: str = Field(min_length=1)
    first_line: str | None = Field(
        default=None, description="Codex rollout metadata for parent verification."
    )
    catchup_end: NonNegativeInt = Field(
        default=0, description="Child transcript byte length at registration time."
    )
    tool_call_id: str | None = Field(
        default=None,
        description="The parent's call a Claude child answers, from the meta file "
        "beside its transcript. An interrupted call's result names no child.",
    )
    description: str | None = Field(
        default=None,
        description="What the parent called a Claude child's task, from the same file.",
    )


class StreamSubagentOpened(BaseModel):
    """Registration result. A Codex rollout belonging to another session is skipped."""

    type: Literal["subagent_opened"] = "subagent_opened"
    conversation_id: UUID7 | None
    offset: NonNegativeInt


class StreamSnapshotStart(BaseModel):
    """Client announces the next transcript range before sending its bytes."""

    type: Literal["snapshot_start"] = "snapshot_start"
    conversation_id: UUID7
    transfer_id: UUID
    start: int = Field(
        ge=0, description="Exclusive end of the previously stored prefix."
    )
    end: int = Field(
        gt=0, description="Exclusive byte offset at a complete-line boundary."
    )


class StreamSnapshotStored(BaseModel):
    """Server acknowledgment after the uploaded snapshot is durably stored."""

    type: Literal["snapshot_stored"] = "snapshot_stored"
    transfer_id: UUID


class StreamFinalize(BaseModel):
    """Server → client: the session ended (`SessionEnd` arrived on the hook pipe) —
    drain the remaining bytes, answer with `eof`, and exit.

    A snapshot starts with `snapshot_start`, followed by binary messages totaling
    `end - start` bytes. The client waits for `snapshot_stored` before sending `eof`.
    Each batch's transcript lines precede its binary messages; they never interleave.
    Batches may upload throughout a turn, before finalization.
    """

    type: Literal["finalize"] = "finalize"


StreamClientMessage = Annotated[
    StreamHello | StreamOpenSubagent | StreamLine | StreamSnapshotStart | StreamEof,
    Field(discriminator="type"),
]
StreamServerMessage = Annotated[
    StreamWelcome | StreamSubagentOpened | StreamFinalize | StreamSnapshotStored,
    Field(discriminator="type"),
]

client_message_adapter: TypeAdapter[StreamClientMessage] = TypeAdapter(
    StreamClientMessage
)
server_message_adapter: TypeAdapter[StreamServerMessage] = TypeAdapter(
    StreamServerMessage
)


class TranscriptKept(BaseModel):
    """How much of a driven session's transcript the server holds. It answers an
    upload: stored, or refused because the bytes did not start where these end."""

    size: int = Field(ge=0, description="Bytes kept; where the next upload starts.")
