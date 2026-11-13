"""Persisted logical volume identities, application associations and retention."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from arcanus.base import TransmuterProxiedMixin
from pydantic import UUID7, JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, MapperArgs, UTCDateTime
from octomate.types.volumes import VolumeKind, VolumeUsage


class Volume(Base, TransmuterProxiedMixin):
    """An application-owned volume; storage and attachments have separate lifetimes."""

    __tablename__ = "volumes"
    __table_args__ = (
        CheckConstraint(
            "(usage IS NOT NULL AND usage = 'tree' AND thread_id IS NOT NULL) OR ((usage IS NULL OR usage != 'tree') AND thread_id IS NULL AND project_id IS NULL)",
            name="ck_volumes_usage_scope",
        ),
        CheckConstraint("kind = 'git' OR base_ref IS NULL", name="ck_volumes_git_ref"),
        Index(
            "uq_volumes_owner_usage",
            "owner_id",
            "usage",
            unique=True,
            sqlite_where=text("usage IN ('memory', 'cache')"),
        ),
        Index(
            "uq_volumes_project_tree",
            "owner_id",
            "thread_id",
            "project_id",
            unique=True,
            sqlite_where=text("usage = 'tree' AND project_id IS NOT NULL"),
        ),
        Index(
            "uq_volumes_chat_tree",
            "owner_id",
            "thread_id",
            unique=True,
            sqlite_where=text("usage = 'tree' AND project_id IS NULL"),
        ),
    )
    __mapper_args__: ClassVar[MapperArgs] = {
        "polymorphic_on": "kind",
        "polymorphic_identity": "directory",
        "with_polymorphic": "*",
    }

    id: Mapped[UUID7] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[UUID7] = mapped_column(
        Uuid,
        ForeignKey("users.id", name="fk_volumes_owner_id_users", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="The registered user who owns this logical volume.",
    )
    storage: Mapped[JsonValue] = mapped_column(
        JSON,
        nullable=False,
        comment="Storage description: kind and storage location, excluding runtime locks, active uses and habitat destinations.",
    )
    kind: Mapped[VolumeKind] = mapped_column(
        String,
        nullable=False,
        comment="Content behavior: a directory or a Git-tracked directory, independent of backing storage.",
    )
    usage: Mapped[VolumeUsage | None] = mapped_column(
        String,
        nullable=True,
        comment="Optional application use: working tree, user memory or package cache.",
    )
    ephemeral: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether contents are discarded after the last active use; the logical identity remains.",
    )
    thread_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey(
            "threads.id", name="fk_volumes_thread_id_threads", ondelete="RESTRICT"
        ),
        nullable=True,
        index=True,
        comment="The thread using this working volume; absent for other uses.",
    )
    project_id: Mapped[UUID7 | None] = mapped_column(
        Uuid,
        ForeignKey(
            "projects.id", name="fk_volumes_project_id_projects", ondelete="RESTRICT"
        ),
        nullable=True,
        index=True,
        comment="The working volume's project; absent for chats and other uses.",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When the logical volume was registered.",
    )
    last_used_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When the application last acquired this volume; used for idle reclamation.",
    )


class GitVolume(Volume):
    """Git-specific initialization policy, independent of the volume's usage."""

    __mapper_args__: ClassVar[MapperArgs] = {"polymorphic_identity": "git"}

    base_ref: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="The Git ref used for the first checkout; None selects the source's default branch.",
    )
