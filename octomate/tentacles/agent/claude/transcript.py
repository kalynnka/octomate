from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from anthropic.types import Message as AnthropicMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from octomate.types.json import JsonObject


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

# Where Claude Code writes session transcripts: the documented
# `<config dir>/projects/<cwd slug>/<session id>.jsonl`.
#
# Plural because `CLAUDE_CONFIG_DIR` relocates the tree and may name several directories,
# comma-separated — reading it as one path would build a single nonsense root out of a
# list and leave that machine's transcripts unfindable. The variable is thinly
# documented, so treat this as the default rather than the law: an operator who lands
# somewhere else overrides it with `agents.claude.transcript_root`.
CLAUDE_PROJECTS_DIRS: tuple[Path, ...] = tuple(
    Path(entry.strip()) / "projects"
    for entry in (
        os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    ).split(",")
    if entry.strip()
)


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


def locate_transcript(session_id: str) -> Path | None:
    """The session's transcript on local disk, newest if a slug moved. A caller with only
    a session id (a web open, or recovery) uses this; a hook already carries the path."""
    matches = sorted(
        (
            match
            for root in CLAUDE_PROJECTS_DIRS
            for match in root.glob(f"*/{session_id}.jsonl")
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None
