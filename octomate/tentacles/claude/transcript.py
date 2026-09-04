from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from anthropic.types import Message as AnthropicMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

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
    timestamp: datetime
    uuid: str
    entrypoint: str


class TranscriptUserMessage(TranscriptSchema):
    role: Literal["user"]
    # Raw blocks, not anthropic `ContentBlockParam`: the input-block union's smart
    # validation lazily wraps a tool-result's inner string content in a char-by-char
    # `ValidatorIterator`, corrupting it. Plain dicts round-trip faithfully.
    content: str | list[JsonObject]


class TranscriptUserLine(TranscriptSessionLine):
    type: Literal["user"]
    message: TranscriptUserMessage
    prompt_id: str = Field(alias="promptId")
    prompt_source: str | None = Field(default=None, alias="promptSource")
    permission_mode: str | None = Field(default=None, alias="permissionMode")
    origin: JsonObject | None = None
    source_tool_assistant_uuid: str | None = Field(
        default=None, alias="sourceToolAssistantUUID"
    )
    tool_use_result: JsonValue = Field(default=None, alias="toolUseResult")


class TranscriptAssistantLine(TranscriptSessionLine):
    type: Literal["assistant"]
    message: AnthropicMessage
    request_id: str | None = Field(default=None, alias="requestId")
    is_api_error_message: bool | None = Field(default=None, alias="isApiErrorMessage")


class TranscriptSystemLine(TranscriptSessionLine):
    type: Literal["system"]
    subtype: str
    level: str
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


class TranscriptAttachmentLine(TranscriptSessionLine):
    type: Literal["attachment"]
    attachment: JsonObject


class TranscriptFileBackup(TranscriptSchema):
    backup_file_name: str | None = Field(alias="backupFileName")
    backup_time: datetime = Field(alias="backupTime")
    version: int


class TranscriptFileHistorySnapshot(TranscriptSchema):
    message_id: str = Field(alias="messageId")
    timestamp: datetime
    tracked_file_backups: dict[str, TranscriptFileBackup] = Field(
        alias="trackedFileBackups"
    )


class TranscriptFileHistorySnapshotLine(TranscriptSchema):
    type: Literal["file-history-snapshot"]
    message_id: str = Field(alias="messageId")
    is_snapshot_update: bool = Field(alias="isSnapshotUpdate")
    snapshot: TranscriptFileHistorySnapshot


class TranscriptQueueOperationLine(TranscriptSchema):
    type: Literal["queue-operation"]
    operation: str
    session_id: str = Field(alias="sessionId")
    timestamp: datetime
    content: str | None = None


class TranscriptAiTitleLine(TranscriptSchema):
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
    type: Literal["permission-mode"]
    session_id: str = Field(alias="sessionId")
    permission_mode: str = Field(alias="permissionMode")


class TranscriptPullRequestLine(TranscriptSchema):
    type: Literal["pr-link"]
    session_id: str = Field(alias="sessionId")
    pr_number: int = Field(alias="prNumber")
    pr_url: str = Field(alias="prUrl")
    pr_repository: str = Field(alias="prRepository")
    timestamp: datetime


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
