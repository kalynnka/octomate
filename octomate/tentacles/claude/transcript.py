"""Typed lines of a Claude Code transcript on disk, and where Claude files them."""

from __future__ import annotations

import logging
import os
import re
import shutil
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal

from anthropic.types import Message as AnthropicMessage
from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)
from pydantic_ai.messages import UserPromptPart

from octomate.schemas.compactions import NativeContext
from octomate.schemas.messages import ModelRequest
from octomate.types.compactions import CompactionTrigger
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)


class TranscriptSchema(BaseModel):
    """A Claude Code transcript value as written on disk."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class TranscriptSessionLine(TranscriptSchema):
    """The envelope shared by session events in a transcript."""

    parent_uuid: str | None = Field(alias="parentUuid")
    is_sidechain: bool = Field(alias="isSidechain")
    user_type: str = Field(alias="userType")
    cwd: str
    session_id: str = Field(alias="sessionId")
    version: str
    git_branch: str = Field(alias="gitBranch")
    timestamp: AwareDatetime
    uuid: str
    entrypoint: str


class CompactSummaryAnchor(TranscriptSchema):
    """The summary message UUID anchoring Claude's retained messages."""

    anchor_uuid: str | None = Field(
        default=None, validation_alias=AliasChoices("anchor_uuid", "anchorUuid")
    )
    uuids: list[str] | None = None
    all_uuids: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("all_uuids", "allUuids")
    )


class CompactMetadata(TranscriptSchema):
    """Completion metadata shared by the SDK and transcript boundary."""

    trigger: CompactionTrigger = "unknown"
    preserved_messages: CompactSummaryAnchor | None = Field(
        default=None,
        validation_alias=AliasChoices("preserved_messages", "preservedMessages"),
    )
    preserved_segment: CompactSummaryAnchor | None = Field(
        default=None,
        validation_alias=AliasChoices("preserved_segment", "preservedSegment"),
    )

    def context(
        self, summary_uuid: str, last_item_id: str | None, omitted: set[str]
    ) -> NativeContext | None:
        """Use the explicit retained UUID list; a partial anchor is not a full list."""
        if last_item_id is None:
            return None
        retained: list[str] = []
        if self.preserved_messages is not None:
            anchor = self.preserved_messages
            if anchor.anchor_uuid != summary_uuid:
                return None
            ids = anchor.uuids if anchor.uuids is not None else anchor.all_uuids
            if ids is None:
                return None
            retained = [id_ for id_ in ids if id_ not in omitted]
        elif self.preserved_segment is not None:
            return None
        return NativeContext(
            last_item_id=last_item_id, replacement_ids=[summary_uuid, *retained]
        )


class CompactBoundary(TranscriptSchema):
    """A completed Claude compaction, identified by its boundary UUID."""

    uuid: str
    session_id: str = Field(validation_alias=AliasChoices("session_id", "sessionId"))
    metadata: CompactMetadata = Field(
        validation_alias=AliasChoices("compact_metadata", "compactMetadata")
    )
    timestamp: AwareDatetime | None = None

    @property
    def summary_uuid(self) -> str | None:
        for anchor in (
            self.metadata.preserved_messages,
            self.metadata.preserved_segment,
        ):
            if anchor is not None and anchor.anchor_uuid is not None:
                return anchor.anchor_uuid
        return None


class TranscriptUserMessage(TranscriptSchema):
    """The `message` of a user line: a plain prompt, or its raw content blocks."""

    role: Literal["user"]
    # Raw blocks, not anthropic `ContentBlockParam`: the input-block union's smart
    # validation lazily wraps a tool-result's inner string content in a char-by-char
    # `ValidatorIterator`, corrupting it. Plain dicts round-trip faithfully.
    content: str | list[JsonObject]

    def compaction_message(
        self, uuid: str, timestamp: AwareDatetime | None
    ) -> ModelRequest:
        part = UserPromptPart(prompt_text(self))
        if timestamp is not None:
            part.timestamp = timestamp
        return ModelRequest(
            parts=[part], timestamp=timestamp, metadata={"native_item_ids": [uuid]}
        )


class TranscriptOrigin(TranscriptSchema):
    """Who wrote a user line when the person did not. Nothing is required, so an
    origin this build does not model never costs the line."""

    kind: str | None = Field(
        default=None, description='"peer" when another agent session wrote the line.'
    )
    sender: str | None = Field(default=None, alias="from")
    name: str | None = None
    body: str | None = None
    handback_report_text: str | None = Field(
        default=None,
        alias="handbackReportText",
        description="A subagent's report, apart from the framing Claude wraps it in.",
    )


class TranscriptUserLine(TranscriptSessionLine):
    """A user line: the prompt opening a turn, or a tool-result line within one."""

    type: Literal["user"]
    message: TranscriptUserMessage
    prompt_id: str = Field(alias="promptId")
    prompt_source: str | None = Field(default=None, alias="promptSource")
    permission_mode: str | None = Field(default=None, alias="permissionMode")
    is_compact_summary: bool = Field(default=False, alias="isCompactSummary")
    origin: TranscriptOrigin | None = None
    source_tool_assistant_uuid: str | None = Field(
        default=None, alias="sourceToolAssistantUUID"
    )
    tool_use_result: JsonValue = Field(default=None, alias="toolUseResult")


class TranscriptAssistantLine(TranscriptSessionLine):
    """An assistant line: the API `Message` Claude returned."""

    type: Literal["assistant"]
    message: AnthropicMessage
    request_id: str | None = Field(default=None, alias="requestId")
    is_api_error_message: bool | None = Field(default=None, alias="isApiErrorMessage")


class TranscriptSystemLine(TranscriptSessionLine):
    """A system line: API retries, hook results, and other session notices."""

    type: Literal["system"]
    subtype: str
    level: str
    compact_metadata: CompactMetadata | None = Field(
        default=None, alias="compactMetadata"
    )
    error: JsonObject | None = None
    max_retries: int | None = Field(default=None, alias="maxRetries")
    retry_attempt: int | None = Field(default=None, alias="retryAttempt")
    retry_in_ms: float | None = Field(default=None, alias="retryInMs")
    has_output: bool | None = Field(default=None, alias="hasOutput")
    hook_additional_context: list[JsonValue] = Field(
        default_factory=list, alias="hookAdditionalContext"
    )
    hook_count: int | None = Field(default=None, alias="hookCount")
    hook_errors: list[JsonValue] = Field(default_factory=list, alias="hookErrors")
    hook_infos: list[JsonValue] = Field(default_factory=list, alias="hookInfos")
    prevented_continuation: bool | None = Field(
        default=None, alias="preventedContinuation"
    )
    stop_reason: str | None = Field(default=None, alias="stopReason")
    tool_use_id: str | None = Field(default=None, alias="toolUseID")


class TranscriptAttachment(TranscriptSchema):
    """What Claude attaches to a turn, as far as this build reads it: a message
    queued in while the turn ran says who wrote it. The rest stays as written."""

    prompt: JsonValue = Field(
        default=None, description="A queued message's text, or its content blocks."
    )
    origin: TranscriptOrigin | None = None


class TranscriptAttachmentLine(TranscriptSessionLine):
    """An attachment line: context the harness adds, or a message queued into the turn."""

    type: Literal["attachment"]
    attachment: TranscriptAttachment


class TranscriptFileBackup(TranscriptSchema):
    """One tracked file's backup in a file-history snapshot."""

    backup_file_name: str | None = Field(alias="backupFileName")
    backup_time: AwareDatetime = Field(alias="backupTime")
    version: int


class TranscriptFileHistorySnapshot(TranscriptSchema):
    """The tracked-file backups taken as of one message."""

    message_id: str = Field(alias="messageId")
    timestamp: AwareDatetime
    tracked_file_backups: dict[str, TranscriptFileBackup] = Field(
        alias="trackedFileBackups"
    )


class TranscriptFileHistorySnapshotLine(TranscriptSchema):
    """A file-history-snapshot line: the tracked-file backups as of one message."""

    type: Literal["file-history-snapshot"]
    message_id: str = Field(alias="messageId")
    is_snapshot_update: bool = Field(alias="isSnapshotUpdate")
    snapshot: TranscriptFileHistorySnapshot


class TranscriptQueueOperationLine(TranscriptSchema):
    type: Literal["queue-operation"]
    operation: str
    session_id: str = Field(alias="sessionId")
    timestamp: AwareDatetime
    content: str | None = None


class TranscriptAiTitleLine(TranscriptSchema):
    """An ai-title line: the title Claude generated for the session."""

    type: Literal["ai-title"]
    session_id: str = Field(alias="sessionId")
    ai_title: str = Field(alias="aiTitle")


class TranscriptLastPromptLine(TranscriptSchema):
    type: Literal["last-prompt"]
    session_id: str = Field(alias="sessionId")
    leaf_uuid: str = Field(alias="leafUuid")
    last_prompt: str | None = Field(default=None, alias="lastPrompt")


class TranscriptModeLine(TranscriptSchema):
    type: Literal["mode"]
    session_id: str = Field(alias="sessionId")
    mode: str


class TranscriptPermissionModeLine(TranscriptSchema):
    """A permission-mode line: the mode the session switched to."""

    type: Literal["permission-mode"]
    session_id: str = Field(alias="sessionId")
    permission_mode: str = Field(alias="permissionMode")


class TranscriptPullRequestLine(TranscriptSchema):
    """A pr-link line: a pull request linked to the session."""

    type: Literal["pr-link"]
    session_id: str = Field(alias="sessionId")
    pr_number: int = Field(alias="prNumber")
    pr_url: str = Field(alias="prUrl")
    pr_repository: str = Field(alias="prRepository")
    timestamp: AwareDatetime


TranscriptLine = Annotated[
    TranscriptUserLine
    | TranscriptAssistantLine
    | TranscriptSystemLine
    | TranscriptAttachmentLine
    | TranscriptFileHistorySnapshotLine
    | TranscriptQueueOperationLine
    | TranscriptAiTitleLine
    | TranscriptLastPromptLine
    | TranscriptModeLine
    | TranscriptPermissionModeLine
    | TranscriptPullRequestLine,
    Field(discriminator="type"),
]

transcript_lines_adapter = TypeAdapter(list[TranscriptLine])

# One typed line at a time, tolerating kinds this module does not model yet
# (`validate_json` raises on an unknown discriminator; those lines are skipped).
transcript_line_adapter: TypeAdapter[TranscriptLine] = TypeAdapter(TranscriptLine)


def prompt_text(message: TranscriptUserMessage) -> str:
    """The plain text of a submitted prompt — the string itself, or the text of its
    text blocks (Claude Code pads a prompt with injected context blocks)."""
    content = message.content
    if isinstance(content, str):
        return content
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def retained_compaction_summaries(
    transcript: bytes, boundaries: list[CompactBoundary]
) -> list[tuple[CompactBoundary, ModelRequest]]:
    """Match a turn's SDK boundaries to their summary records in one JSONL pass."""
    pending = {
        (boundary.session_id, boundary.uuid): boundary for boundary in boundaries
    }
    summaries: list[tuple[CompactBoundary, ModelRequest]] = []
    for raw in BytesIO(transcript):
        if b'"isCompactSummary"' not in raw:
            continue
        try:
            line = TranscriptUserLine.model_validate_json(raw)
        except ValidationError:
            continue
        if not line.is_compact_summary or line.is_sidechain or line.parent_uuid is None:
            continue
        key = (line.session_id, line.parent_uuid)
        boundary = pending.get(key)
        if boundary is not None and boundary.summary_uuid in (None, line.uuid):
            summaries.append(
                (boundary, line.message.compaction_message(line.uuid, line.timestamp))
            )
            del pending[key]
        if not pending:
            break
    return summaries


def transcripts_dir(cwd: Path) -> Path:
    """Where Claude Code files the sessions run in `cwd`: under its config dir,
    a directory named by the cwd with every character outside `[A-Za-z0-9]`
    replaced by `-`, holding `<session id>.jsonl` and the session's subagents
    under `<session id>/`."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def relocate_session(session_id: str, *, cwd: Path) -> None:
    """Carry a Claude session to the cwd it will resume in.

    Claude files a session under the cwd it ran in and resumes it only from there
    — from anywhere else the CLI exits with "No conversation found". A thread that
    teleported into a project's workspace resumes in another directory, so its
    transcript, and its subagents beside it, move to that directory's own dir
    first. A session with no transcript anywhere cannot be resumed, and says so.

    TODO: rebuild the transcript from the conversation's own ledger
    instead of moving a local file, so a native session — whose transcript is on
    its own machine — can be carried too; and validate the rebuilt bytes against
    the original, since Claude's prompt cache survives only a transcript that
    replays exactly the messages it was built from.
    """
    target = transcripts_dir(cwd)
    projects = target.parent
    found = list(projects.glob(f"*/{session_id}.jsonl"))
    if len(found) != 1:
        raise FileNotFoundError(
            f"session {session_id} has {len(found)} transcripts under {projects}; "
            "it can be resumed from exactly one"
        )
    [transcript] = found
    source = transcript.parent
    logger.info("session %s: carrying its transcript to %s", session_id, target)
    target.mkdir(parents=True, exist_ok=True)
    shutil.move(transcript, target / transcript.name)
    if (source / session_id).is_dir():
        shutil.move(source / session_id, target / session_id)
