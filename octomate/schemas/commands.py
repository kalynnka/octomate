"""Runtime command catalogs and explicit invocations, separate from chat text."""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, SerializeAsAny, model_validator

from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.segments import FileSegment, ImageSegment, MessageSegment


@dataclass(frozen=True)
class CommandContext:
    """Server-resolved context shared by discovery and execution.

    The caller authorizes the user and resolves the selected conversation and
    effective workspace before calling an adapter. A native transcript's session
    handle alone does not grant permission to drive that runtime.
    """

    user_id: Annotated[
        uuid.UUID,
        Field(description="The authenticated Octomate user, not a platform sender id."),
    ]
    address: Annotated[
        ChannelAddress,
        Field(description="The originating surface and its platform user."),
    ]
    cwd: Annotated[
        Path,
        Field(
            description="The effective runtime workspace, never a client-supplied path."
        ),
    ]
    conversation: Annotated[
        Conversation | None,
        Field(description="None while composing a new conversation."),
    ]
    model: Annotated[
        str | None,
        Field(description="The selected runtime model, resolved by the host."),
    ] = None
    permission_mode: Annotated[
        str | None, Field(description="The effective runtime approval posture.")
    ] = None


class CommandDescriptor(BaseModel):
    """The fields channels consume for any invocable runtime entry.

    Tentacles subclass this model for typed runtime metadata. Catalog serialization
    preserves those fields, but channels depend only on this base. Rehydrate an
    extended descriptor with its tentacle's concrete model, not this base model.
    """

    id: str = Field(
        min_length=1,
        description="Opaque identity, stable and unique within the agent's scoped "
        "catalog. The tentacle maps it to the upstream invocation.",
    )
    name: str = Field(min_length=1, description="Upstream command or skill name.")
    description: str = Field(description="Upstream description, preserved verbatim.")
    argument_hint: str | None = Field(
        default=None, description="Upstream free-form input hint, when provided."
    )
    accepts_attachments: bool | None = Field(
        default=None,
        description="Whether attached input is supported; null means unspecified. "
        "Channels offer attachments only when this is explicitly true.",
    )


class ReadyCommandCatalog(BaseModel):
    """A successful discovery, including an explicitly empty catalog."""

    status: Literal["ready"] = "ready"
    entries: list[SerializeAsAny[CommandDescriptor]] = Field(
        description="Commands, skills and other invocable entries in upstream order; "
        "runtime-specific descriptor subclasses retain their additional fields.",
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="User-visible discovery gaps, such as a skill catalog without "
        "native slash-command discovery.",
    )

    @model_validator(mode="after")
    def unique_entry_ids(self) -> Self:
        """One selection must identify exactly one entry, even when names repeat."""
        ids = [entry.id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("command catalog entry ids must be unique")
        return self


class LoadingCommandCatalog(BaseModel):
    """The runtime has not finished discovering entries for this context."""

    status: Literal["loading"] = "loading"


class UnavailableCommandCatalog(BaseModel):
    """Discovery cannot currently supply a catalog, with a user-facing reason."""

    status: Literal["unsupported", "unavailable", "failed"] = Field(
        description="No discovery capability, an unmet prerequisite, or a failed probe."
    )
    message: str = Field(min_length=1, description="Why discovery cannot proceed.")


CommandCatalog = Annotated[
    ReadyCommandCatalog | LoadingCommandCatalog | UnavailableCommandCatalog,
    Field(discriminator="status"),
]


class AgentCommandCatalog(BaseModel):
    """One discovery surface for the selected agent, regardless of runtime vocabulary."""

    agent_id: str = Field(
        min_length=1, description="The configured agent tentacle owning the catalog."
    )
    catalog: CommandCatalog = Field(
        description="Discovery state and available invocations in this context."
    )


class CommandInvocation(BaseModel):
    """Explicit command intent; ordinary chat text does not use this contract.

    The caller must revalidate catalog membership and attachment support before dispatch.
    Attachments here are already resolved by the host, not arbitrary client paths.
    """

    command_id: str = Field(
        min_length=1, description="The selected descriptor's opaque id."
    )
    arguments: str = Field(
        default="",
        description="Raw argument text, without trimming or prompt decoration.",
    )
    attachments: list[
        Annotated[ImageSegment | FileSegment, Field(discriminator="type")]
    ] = Field(default_factory=list, description="Resolved runtime input attachments.")


class CommandResult(BaseModel):
    """A direct command's user-visible output, without a fabricated model turn."""

    status: Literal["completed"] = "completed"
    segments: list[MessageSegment] = Field(
        default_factory=list,
        description="Output to present on the originating surface.",
    )


class CommandError(BaseModel):
    """An explicit refusal or failure; none of these outcomes falls back to chat."""

    status: Literal[
        "unsupported", "unknown", "stale", "busy", "unavailable", "failed"
    ] = Field(description="The reason this invocation did not complete successfully.")
    message: str = Field(
        min_length=1, description="An actionable explanation for the user."
    )


CommandOutcome = Annotated[CommandResult | CommandError, Field(discriminator="status")]
