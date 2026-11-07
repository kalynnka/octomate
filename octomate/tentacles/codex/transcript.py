"""Typed lines of a Codex rollout file, and the runtime's own tree."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from pydantic_ai.messages import CompactionPart, UserPromptPart

from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.tentacles.codex.adapter import CODEX_PROVIDER_NAME, codex_metadata
from octomate.tentacles.codex.schemas import PermissionProfile
from octomate.types.json import JsonObject
from octomate.types.permissions import CodexPermissionMode

# Codex's own tree (`CODEX_HOME` relocates it): a workspace root under it is the
# runtime's per-session storage, never a project.
CODEX_HOME_DIRS: tuple[Path, ...] = (
    Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
)


class RolloutLine(BaseModel):
    """One line of a Codex rollout: its timestamp, record type, and raw payload."""

    model_config = ConfigDict(extra="ignore")

    timestamp: AwareDatetime
    type: Literal[
        "session_meta",
        "turn_context",
        "event_msg",
        "response_item",
        "world_state",
        "compacted",
        "inter_agent_communication_metadata",
    ]
    payload: JsonObject


rollout_line_adapter = TypeAdapter(RolloutLine)


class CompactedItem(BaseModel):
    """A committed context replacement, independent of turn lifecycle events."""

    message: str = Field(description="Readable summary; empty for opaque compaction.")
    replacement_history: list[JsonObject] | None = Field(
        default=None,
        description="Native replacement items, including retained context and provider checkpoints.",
    )
    compaction_response_id: str | None = Field(
        default=None,
        description="Provider response ID of the compaction request, when reported.",
    )

    def to_messages(
        self, timestamp: AwareDatetime
    ) -> list[ModelRequest | ModelResponse]:
        """Normalize created summaries/checkpoints; retained messages keep their owner."""
        checkpoints = [
            compaction_item_adapter.validate_python(raw)
            for raw in self.replacement_history or []
            if raw.get("type")
            in {"compaction", "compaction_summary", "context_compaction"}
        ]
        messages: list[ModelRequest | ModelResponse] = []
        if self.message:
            messages.append(
                ModelRequest(
                    parts=[UserPromptPart(self.message, timestamp=timestamp)],
                    timestamp=timestamp,
                    metadata=codex_metadata(),
                )
            )
        messages.extend(
            ModelResponse(
                parts=[
                    CompactionPart(
                        id=checkpoint.id,
                        provider_name=CODEX_PROVIDER_NAME,
                        provider_details={
                            "encrypted_content": checkpoint.encrypted_content
                        }
                        if checkpoint.encrypted_content is not None
                        else None,
                    )
                ],
                timestamp=timestamp,
                provider_name=CODEX_PROVIDER_NAME,
                provider_response_id=self.compaction_response_id,
                metadata=codex_metadata(),
            )
            for checkpoint in checkpoints
        )
        return messages


class CompletedCompactionItem(BaseModel):
    """The native rollout's completed context-compaction item."""

    type: Literal["ContextCompaction"]
    id: str


class CompactionCompletedEvent(BaseModel):
    """The source item identity following a committed replacement."""

    type: Literal["item_completed"]
    thread_id: str
    turn_id: str
    item: CompletedCompactionItem


class EncryptedCompactionItem(BaseModel):
    """An opaque provider checkpoint carried in replacement history."""

    type: Literal["compaction", "compaction_summary"]
    id: str | None = None
    encrypted_content: str


class ContextCompactionItem(BaseModel):
    """A context-compaction item that may omit its encrypted content."""

    type: Literal["context_compaction"]
    id: str | None = None
    encrypted_content: str | None = None


compaction_item_adapter: TypeAdapter[
    EncryptedCompactionItem | ContextCompactionItem
] = TypeAdapter(
    Annotated[
        EncryptedCompactionItem | ContextCompactionItem,
        Field(discriminator="type"),
    ]
)


class ParsedCommand(BaseModel):
    """Codex's classification of one command in a native execution item."""

    type: Literal["read", "list_files", "search", "unknown"]
    cmd: str | None = Field(
        default=None, description="Command text classified by Codex."
    )
    name: str | None = Field(default=None, description="Display name of the read file.")
    path: str | None = Field(
        default=None, description="File or directory targeted by the command."
    )
    query: str | None = Field(
        default=None, description="Search query recorded by Codex."
    )


class CompletedCommandItem(BaseModel):
    """An executed command recorded separately from its outer tool call."""

    type: Literal["CommandExecution"]
    id: str
    command: list[str]
    cwd: str
    parsed_cmd: list[ParsedCommand]
    status: Literal["completed", "failed", "declined"]
    aggregated_output: str


class UserMessageContent(BaseModel):
    """A user-role content part; only input text is imported."""

    type: str
    text: str | None = None


class UserMessageMetadata(BaseModel):
    """Codex's provenance labels, aligned with the message's content parts."""

    turn_id: str
    content_item_kinds: list[str]


class UserMessageItem(BaseModel):
    """A current-format user message with explicit per-part provenance.

    User-role records also carry injected context. Only parts labelled
    `user.text` are human text; unlabelled records rely on the legacy event.
    """

    type: Literal["message"]
    role: Literal["user"]
    content: list[UserMessageContent]
    metadata: UserMessageMetadata = Field(
        alias="internal_chat_message_metadata_passthrough"
    )


class ThreadSpawnMetadata(BaseModel):
    """A child rollout's `thread_spawn` metadata, naming the parent thread."""

    model_config = ConfigDict(extra="ignore")

    parent_thread_id: str
    depth: int = 1
    agent_path: str = ""


class SubagentSource(BaseModel):
    """The `subagent` block of a session's `source`, present on a spawned child."""

    model_config = ConfigDict(extra="ignore")

    thread_spawn: ThreadSpawnMetadata | None = None


class SessionSource(BaseModel):
    """A structured `session_meta` `source`, carrying a spawned child's lineage."""

    model_config = ConfigDict(extra="ignore")

    subagent: SubagentSource | None = None


class SessionMetadata(BaseModel):
    """The payload of a rollout's opening `session_meta` line."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    id: str | None = None
    # The directory the session opened in. Codex repeats it per turn in a
    # `turn_context` line, which across every rollout on hand never disagrees with
    # this one, so the session's own report is the whole of where its turns ran.
    cwd: str = ""
    originator: str | None = None
    source: str | SessionSource | None = None
    thread_source: str | None = None


session_metadata_adapter = TypeAdapter(SessionMetadata)


class WorkspaceWritePolicy(BaseModel):
    """The workspace sandbox representable by Octomate's review presets."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["workspace-write"]
    writable_roots: list[str] = Field(default_factory=list, max_length=0)
    network_access: Literal[False] = False
    exclude_tmpdir_env_var: bool = False
    exclude_slash_tmp: bool = False


class FullAccessPolicy(BaseModel):
    """The unrestricted sandbox used by the full-access preset."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["danger-full-access"]


class ActivePermissionProfile(BaseModel):
    """Native profiles whose boundaries Octomate can reproduce on a fork."""

    id: Literal[":workspace"] | PermissionProfile = Field(
        description="The built-in legacy profile or an Octomate-selected native profile."
    )


class TurnPermissions(BaseModel):
    """Permissions recorded in a native rollout's turn_context payload."""

    approval_policy: Literal["on-request", "never"]
    approvals_reviewer: Literal["user", "auto_review"] = "user"
    sandbox_policy: WorkspaceWritePolicy | FullAccessPolicy = Field(
        discriminator="type"
    )
    active_permission_profile: ActivePermissionProfile | None = Field(
        default=None, description="Selected native profile, absent in legacy rollouts."
    )

    @model_validator(mode="after")
    def profile_matches_sandbox(self) -> Self:
        profile = self.active_permission_profile
        if isinstance(self.sandbox_policy, FullAccessPolicy):
            if profile is not None and profile.id != ":danger-full-access":
                raise ValueError("Native profile does not match sandbox_policy")
            return self
        strict = profile is not None and profile.id == "octomate-workspace"
        flags = (
            self.sandbox_policy.exclude_tmpdir_env_var,
            self.sandbox_policy.exclude_slash_tmp,
        )
        if flags != (strict, strict) or (
            profile is not None
            and profile.id not in {":workspace", "octomate-workspace"}
        ):
            raise ValueError("Native profile does not match sandbox_policy")
        return self

    @property
    def permission_mode(self) -> CodexPermissionMode:
        if isinstance(self.sandbox_policy, FullAccessPolicy):
            if self.approval_policy == "never":
                return "full_access"
        elif self.approval_policy == "on-request":
            return (
                "auto_review"
                if self.approvals_reviewer == "auto_review"
                else "user_review"
            )
        raise ValueError("Native Codex permissions do not match an Octomate preset")


def payload_type(line: RolloutLine) -> str | None:
    value = line.payload.get("type")
    return value if isinstance(value, str) else None
