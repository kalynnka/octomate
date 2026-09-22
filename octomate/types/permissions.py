from __future__ import annotations

from typing import Literal, get_args

from claude_agent_sdk import PermissionMode as ClaudePermissionMode
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypeIs

# What Inkling implements, from Claude's scale:
#
# - `default`      park every deferral for a human, and wait
# - `dontAsk`      resolve questions in-process; the agent decides and says so
# - `bypassPermissions`  grant approvals without a card
#
# `plan` and `auto` are Claude Code behaviors with nothing to resolve to here.
# `acceptEdits` is absent because nothing distinguishes an edit: the harness scopes
# `FileSystem` and `Shell` with allow/deny lists rather than approval gates, so no
# Inkling tool sets `requires_approval` and there is no edit approval to accept.
InklingPermissionMode = Literal["default", "dontAsk", "bypassPermissions"]

# Codex UI presets combine SDK approval and sandbox settings.
CodexPermissionMode = Literal["user_review", "auto_review", "full_access"]

# Harness-defined names are persisted verbatim. Validate new selections against the
# running tentacle, not while loading historical conversations.
DeepseekPermissionMode = str
AgentPermissionMode = str


class PermissionMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1, description="The mode id sent to the agent.")
    name: str = Field(
        min_length=1, description="The agent's display name for this mode."
    )
    description: str | None = Field(default=None, description="What this mode permits.")


def is_claude_mode(mode: str | None) -> TypeIs[ClaudePermissionMode]:
    return mode in get_args(ClaudePermissionMode)
