from __future__ import annotations

import uuid
from pathlib import Path

from arcanus.base import TransmuterProxiedMixin
from pydantic import JsonValue
from sqlalchemy import JSON, Boolean, String, UniqueConstraint, Uuid, true
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from octomate.models.base import Base, PathString


class Project(Base, TransmuterProxiedMixin):
    """A project: a code location Octomate knows by name, declared in the operator's
    ``projects:`` block. Note that `projects` here has nothing to do with
    `~/.claude/projects/`, which is where Claude Code stores transcripts."""

    __tablename__ = "projects"
    # Named, so the constraint alembic generates carries the name it can drop again.
    __table_args__ = (UniqueConstraint("root", name="uq_projects_root"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        unique=True,
        comment="Stable name for this project; defaults to its root's directory name.",
    )
    root: Mapped[Path] = mapped_column(
        PathString,
        nullable=False,
        comment=(
            "The directory this project is, as an absolute local path. The identity: "
            "one project per root, and a root that has since been deleted keeps its "
            "row, because the runs recorded under it still name it."
        ),
    )
    # No `PathString` here: pydantic serializes a `Path` to its string natively, so the
    # engine's JSON serializer stores text, and the transmuter validates it back.
    extra_roots: Mapped[list[Path]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="Further directories that are also this project, as absolute paths.",
    )
    upstream: Mapped[JsonValue] = mapped_column(
        JSON,
        nullable=False,
        comment=(
            "Where this project's mirror comes from and how it is kept current: kind "
            "`remote` carries a url that is cloned and fetched; kind `directory` "
            "carries a path that is `git init`'d and synced by copying in and "
            "committing."
        ),
    )
    description: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="What this project is.",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
        comment=(
            "Whether this project is part of the working set. Reconciliation clears it "
            "for a root that is no longer on disk, so the row survives for the runs "
            "and threads that name it while nothing new resolves to it."
        ),
    )
