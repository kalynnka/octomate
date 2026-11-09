"""The volumes table: the folders in a user's root that runs mount, polymorphic on
what each one holds."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime
from octomate.types.volumes import VolumeKind


class Volume(Base, TransmuterProxiedMixin):
    """One folder in a user's root; the polymorphic base keyed on `kind`.

    The row says that the folder exists, who owns it and where it came from. Its
    contents, git state and sizes stay on disk. The row is written before the folder
    and deleted after it, so a folder never exists without a row that owns it.
    """

    __tablename__ = "volumes"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'tree') = (thread_id IS NOT NULL)",
            name="ck_volumes_tree_has_thread",
        ),
        CheckConstraint(
            "kind = 'tree' OR (project_id IS NULL AND base_ref IS NULL)",
            name="ck_volumes_tree_only_fields",
        ),
        Index(
            "uq_volumes_owner_kind",
            "owner_id",
            "kind",
            unique=True,
            sqlite_where=text("kind != 'tree'"),
        ),
    )
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_abstract": True,
        "with_polymorphic": "*",
    }

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("users.id", name="fk_volumes_owner_id_users", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="The registered user whose root holds this volume.",
    )
    kind: Mapped[VolumeKind] = mapped_column(
        String,
        nullable=False,
        comment="What the volume holds: tree, memory or cache. Never changed.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When the row was written, before its folder was made.",
    )
    last_used_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When a run last mounted this volume; the idle sweep reads it.",
    )


class TreeVolume(Volume):
    """One thread's working copy of one project, forked from the project's mirror;
    a thread in no project gets a tree with no project."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "tree"}

    thread_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey(
            "threads.id", name="fk_volumes_thread_id_threads", ondelete="RESTRICT"
        ),
        nullable=True,
        index=True,
        comment="The thread this tree is for. Set on trees only.",
    )
    project_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey(
            "projects.id", name="fk_volumes_project_id_projects", ondelete="RESTRICT"
        ),
        nullable=True,
        index=True,
        comment="The project whose mirror this tree was forked from; NULL for a "
        "thread in no project.",
    )
    base_ref: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="The mirror ref this tree was forked from; NULL for the mirror's "
        "default branch or a tree in no project.",
    )


# Declared after `TreeVolume`, whose columns they index. NULL never equals NULL, so
# a tree in no project needs its own index to stay one per owner and thread.
Index(
    "uq_volumes_project_tree",
    Volume.owner_id,
    TreeVolume.thread_id,
    TreeVolume.project_id,
    unique=True,
    sqlite_where=text("kind = 'tree' AND project_id IS NOT NULL"),
)
Index(
    "uq_volumes_chat_tree",
    Volume.owner_id,
    TreeVolume.thread_id,
    unique=True,
    sqlite_where=text("kind = 'tree' AND project_id IS NULL"),
)


class MemoryVolume(Volume):
    """A user's harness memory, one per user, mounted into every run."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "memory"}


class CacheVolume(Volume):
    """A user's package caches, one per user, mounted into every run."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "cache"}
