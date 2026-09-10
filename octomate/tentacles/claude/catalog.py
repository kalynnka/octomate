"""The model metadata in Claude Code's initialize response."""

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ClaudeModelInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: str
    display_name: str
    description: str | None = None
    supports_effort: bool | None = None
    supported_effort_levels: list[str] | None = None


class ClaudeAccountInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_provider: str | None = None


class ClaudeServerInfo(BaseModel):
    models: list[ClaudeModelInfo] = Field(min_length=1)
    account: ClaudeAccountInfo = Field(default_factory=ClaudeAccountInfo)
