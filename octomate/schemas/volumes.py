"""Volumes: the folders in a user's root that runs mount."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import UUID7, AwareDatetime, ConfigDict, Field
from uuid_utils.compat import uuid7

from octomate.models import volumes as volume_models
from octomate.schemas.base import sqlalchemy_materia


@sqlalchemy_materia.bless(volume_models.Volume)
class Volume(BaseTransmuter):
    """One folder in a user's root, polymorphic on what it holds."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[UUID7, Identity] = Field(default_factory=uuid7, frozen=True)
    owner_id: UUID7 = Field(
        frozen=True, description="The registered user whose root holds this volume."
    )
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        frozen=True,
        description="When the row was written, before its folder was made.",
    )
    last_used_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When a run last mounted this volume; the idle sweep reads it.",
    )


@sqlalchemy_materia.bless(volume_models.TreeVolume)
class TreeVolume(Volume):
    """One thread's working copy of one project; a thread in no project gets a
    tree with no project."""

    kind: Literal["tree"] = "tree"
    thread_id: UUID7 = Field(frozen=True, description="The thread this tree is for.")
    project_id: UUID7 | None = Field(
        default=None,
        frozen=True,
        description="The project whose mirror this tree was forked from; None for a "
        "thread in no project.",
    )
    base_ref: str | None = Field(
        default=None,
        frozen=True,
        description="The mirror ref this tree was forked from; None for the mirror's "
        "default branch or a tree in no project.",
    )


@sqlalchemy_materia.bless(volume_models.MemoryVolume)
class MemoryVolume(Volume):
    """A user's harness memory, one per user, mounted into every run."""

    kind: Literal["memory"] = "memory"


@sqlalchemy_materia.bless(volume_models.CacheVolume)
class CacheVolume(Volume):
    """A user's package caches, one per user, mounted into every run."""

    kind: Literal["cache"] = "cache"
