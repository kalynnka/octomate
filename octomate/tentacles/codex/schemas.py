"""Codex model and command metadata and native protocol extensions."""

from pathlib import Path
from typing import Literal, NamedTuple

from openai_codex.generated.v2_all import Model, ReasoningEffort, SkillScope
from openai_codex.generated.v2_all import ThreadResumeParams as SdkThreadResumeParams
from openai_codex.generated.v2_all import ThreadStartParams as SdkThreadStartParams
from openai_codex.generated.v2_all import TurnStartParams as SdkTurnStartParams
from pydantic import BaseModel, Field

from octomate.schemas.commands import CommandDescriptor

type PermissionProfile = Literal[
    "octomate-workspace", ":read-only", ":danger-full-access"
]


class ThreadStartParams(SdkThreadStartParams):
    """Native thread start with the permission-profile field omitted by the SDK."""

    permissions: PermissionProfile = Field(
        description="Native permission profile to select."
    )


class ThreadResumeParams(SdkThreadResumeParams):
    """Native thread resume with the permission-profile field omitted by the SDK."""

    permissions: PermissionProfile = Field(
        description="Native permission profile to select."
    )


class TurnStartParams(SdkTurnStartParams):
    """Native turn start with the permission-profile field omitted by the SDK."""

    permissions: PermissionProfile = Field(
        description="Native permission profile to select."
    )


class CodexModelCatalog(NamedTuple):
    """Visible native models and the configured provider and defaults."""

    provider: str
    models: list[Model]
    default_model: str | None
    configured_effort: ReasoningEffort | None


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
