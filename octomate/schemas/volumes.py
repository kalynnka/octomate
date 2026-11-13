"""Logical volumes: persisted identity and policy, independent of their backing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import UUID7, AwareDatetime, ConfigDict, Field, model_validator
from uuid_utils.compat import uuid7

from octomate.models import volumes as volume_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.volumes import VolumeKind, VolumeUsage


@sqlalchemy_materia.bless(volume_models.Volume)
class Volume(BaseTransmuter):
    """A logical volume whose identity outlives any local directory or attachment.

    Operations change this transmuter directly. The caller's session owns
    persistence; paths, mounts and active uses belong to the runtime.
    """

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    owner_id: UUID7 = Field(
        frozen=True, description="The registered user who owns this logical volume."
    )
    kind: VolumeKind = Field(
        default="directory",
        frozen=True,
        description="Content behavior: a directory or a Git-tracked directory, independent of backing storage.",
    )
    usage: VolumeUsage | None = Field(
        default=None,
        frozen=True,
        description="Optional application use: working tree, user memory or package cache.",
    )
    ephemeral: bool = Field(
        default=False,
        frozen=True,
        description="Whether contents are discarded after the last active use; the logical identity remains.",
    )
    thread_id: UUID7 | None = Field(
        default=None,
        frozen=True,
        description="The thread using this working volume; absent for other uses.",
    )
    project_id: UUID7 | None = Field(
        default=None,
        frozen=True,
        description="The working volume's project; absent for chats and other uses.",
    )
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        frozen=True,
        description="When the logical volume was registered.",
    )
    last_used_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the application last acquired this volume; used for idle reclamation.",
    )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.usage == "tree":
            if self.thread_id is None:
                raise ValueError("A working-tree volume requires a thread")
        elif self.thread_id is not None or self.project_id is not None:
            raise ValueError(
                "Only working-tree volumes have thread or project associations"
            )
        return self

    def touch(self) -> None:
        """Record application use; the caller commits it with the owning session."""
        self.last_used_at = datetime.now(UTC)


@sqlalchemy_materia.bless(volume_models.GitVolume)
class GitVolume(Volume):
    """Git content and reference policy, usable without a thread or project."""

    kind: Literal["git"] = Field(default="git", frozen=True)
    base_ref: str | None = Field(
        default=None,
        frozen=True,
        description="The Git ref used for the first checkout; None selects the source's default branch.",
    )

    @property
    def snapshot_ref(self) -> str:
        """Stable private snapshot identity, preserved when backing is reclaimed."""
        return f"refs/octomate/trees/{self.id}"

    @property
    def branch(self) -> str:
        """Initial branch, preserving the existing spelling for thread volumes."""
        if self.thread_id is not None:
            return f"octomate/thread-{self.thread_id}"
        return f"octomate/volume-{self.id}"
