"""Model and command metadata in Claude Code's initialize response."""

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from octomate.schemas.commands import CommandDescriptor


class ClaudeCommandDescriptor(CommandDescriptor, frozen=True):
    """A native initialize command normalized for the shared catalog."""

    id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("id", "name"),
        description="Claude's canonical command name, used for invocation.",
    )
    argument_hint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("argument_hint", "argumentHint"),
        description="The runtime's argument hint, including an empty hint.",
    )
    aliases: tuple[str, ...] = Field(
        default=(), description="Additional command names advertised by Claude."
    )


class ClaudeModelInfo(BaseModel):
    """One model in Claude Code's catalog, as its initialize response lists it."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: str
    display_name: str
    description: str | None = None
    supports_effort: bool | None = None
    supported_effort_levels: list[str] | None = None


class ClaudeAccountInfo(BaseModel):
    """The account block of Claude Code's initialize response."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    api_provider: str | None = None


class ClaudeServerInfo(BaseModel):
    """Claude Code's initialize response, cached by the SDK during connection."""

    models: list[ClaudeModelInfo] = Field(min_length=1)
    account: ClaudeAccountInfo = Field(default_factory=ClaudeAccountInfo)
    commands: list[ClaudeCommandDescriptor] | None = Field(
        default=None,
        description="Native command metadata; omitted means discovery is unsupported, "
        "while an empty list is a successful empty catalog.",
    )
