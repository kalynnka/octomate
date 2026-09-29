"""Runtime command catalogs and explicit invocations, separate from chat text."""

import uuid
from dataclasses import dataclass, replace
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

    agent_id: Annotated[
        str,
        Field(min_length=1, description="The configured agent owning this context."),
    ]
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

    def matches(self, other: "CommandContext") -> bool:
        """Whether a resolved context can reuse this context's catalog."""
        conversation = self.conversation
        other_conversation = other.conversation
        return (
            self.agent_id == other.agent_id
            and self.user_id == other.user_id
            and self.address == other.address
            and self.cwd == other.cwd
            and self.model == other.model
            and self.permission_mode == other.permission_mode
            and (
                (conversation is None and other_conversation is None)
                or (
                    conversation is not None
                    and other_conversation is not None
                    and conversation.id == other_conversation.id
                    and conversation.external_id == other_conversation.external_id
                )
            )
        )


class CommandDescriptor(BaseModel, frozen=True):
    """The fields channels consume for any invocable runtime entry.

    Tentacles subclass this model for typed runtime metadata. Catalog serialization
    preserves those fields, but channels depend only on this base. Rehydrate an
    extended descriptor with its tentacle's concrete model, not this base model.
    Descriptors are frozen; runtime extensions must use hashable field values.
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


class CommandCatalog(BaseModel):
    """Runtime descriptors and their discovery state in one resolved context."""

    context: CommandContext = Field(
        description="The context this catalog was discovered in."
    )
    descriptors: set[SerializeAsAny[CommandDescriptor]] = Field(
        default_factory=set,
        description="Unique invocable runtime entries, with no ordering guarantee. Runtime-specific "
        "descriptor subclasses retain their additional fields; empty unless ready.",
    )
    status: Literal["ready", "loading", "unsupported", "unavailable", "failed"] = Field(
        description="Discovery state; ready with no descriptors is a successful empty catalog."
    )
    message: str | None = Field(
        default=None,
        min_length=1,
        description="Required explanation when discovery cannot supply a catalog.",
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="User-visible discovery gaps, such as skills without native slash commands.",
    )

    @model_validator(mode="after")
    def valid_discovery_state(self) -> Self:
        """Only ready catalogs contain descriptors; failed discovery explains why."""
        if self.status != "ready" and self.descriptors:
            raise ValueError("only ready catalogs may contain descriptors")
        if self.status in {"unsupported", "unavailable", "failed"} and not self.message:
            raise ValueError("unavailable catalogs require a message")
        ids = [descriptor.id for descriptor in self.descriptors]
        if len(ids) != len(set(ids)):
            raise ValueError("command catalog descriptor ids must be unique")
        return self

    def snapshot(self) -> Self:
        """Copy catalog data without deep-copying the conversation's database state."""
        conversation = self.context.conversation
        return self.model_copy(
            update={
                "context": replace(
                    self.context,
                    conversation=conversation.model_copy()
                    if conversation is not None
                    else None,
                ),
                "descriptors": {
                    descriptor.model_copy(deep=True) for descriptor in self.descriptors
                },
                "limitations": list(self.limitations),
            }
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
