from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from anthropic.types import ContentBlockParam, Message as AnthropicMessage
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
    content: str | list[ContentBlockParam]


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
    is_api_error_message: bool | None = Field(
        default=None, alias="isApiErrorMessage"
    )


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
