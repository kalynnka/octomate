"""Codex command metadata and native protocol extensions."""

from pathlib import Path

from openai_codex.generated.v2_all import SkillScope
from pydantic import BaseModel, Field

from octomate.schemas.commands import CommandDescriptor


class ThreadSettingsUpdateResponse(BaseModel):
    """Native acknowledgement that a thread settings update was queued.

    The SDK omits this experimental endpoint. Codex emits
    thread/settings/updated when the settings have been applied for future turns.
    """


class CodexCommandDescriptor(CommandDescriptor, frozen=True):
    """An enabled Codex skill, identified by its native invocation path."""

    path: Path = Field(description="The native skill path used for invocation.")
    scope: SkillScope = Field(description="The skill's native discovery scope.")
    plugin_id: str | None = Field(
        default=None, description="The owning Codex plugin ID, when supplied."
    )
