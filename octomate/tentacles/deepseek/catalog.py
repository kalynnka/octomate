"""DSH command descriptors from the agent-scoped Remote registry."""

from pydantic import AliasChoices, AliasPath, Field, TypeAdapter

from octomate.schemas.commands import CommandDescriptor


class DeepseekCommandDescriptor(CommandDescriptor, frozen=True):
    """Native command metadata normalized for channel discovery."""

    id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("id", "name"),
        description="The command name, unique in the receiving agent's registry.",
    )
    argument_hint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("argument_hint", AliasPath("input", "hint")),
        description="The native free-form input hint, when input is declared.",
    )
    accepts_attachments: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "accepts_attachments", AliasPath("input", "attachments")
        ),
        description="DSH rejects attachments unless the definition opts in.",
    )
    definition_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("definition_id", "definitionId"),
        description="Plugin-owned identity of the effective definition, when provided.",
    )


FORK_COMMAND: DeepseekCommandDescriptor = DeepseekCommandDescriptor(
    id="fork",
    name="fork",
    description="Copy this conversation into an independent thread.",
    unavailable_reason="DSH cannot relocate a fork into an independent Octomate workspace yet.",
)


command_descriptors_adapter = TypeAdapter(list[DeepseekCommandDescriptor])
