from __future__ import annotations

import uuid
from pathlib import Path

from arcanus.base import TransmuterProxiedMixin
from sqlalchemy import JSON, Dialect, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator
from uuid_utils.compat import uuid7

from octomate.models.base import Base


class PathString(TypeDecorator[Path]):
    """A filesystem path, stored as text.

    A root is a `Path` everywhere above this line. The driver binds only str, bytes,
    int, float and None, so the conversion belongs here rather than at every caller.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Path | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Path | None:
        return None if value is None else Path(value)


class Project(Base, TransmuterProxiedMixin):
    """A declared project. Note that `projects` here is the operator's registry of
    code locations, and has nothing to do with `~/.claude/projects/`, which is where
    Claude Code stores transcripts (`CLAUDE_PROJECTS_DIRS`)."""

    __tablename__ = "projects"

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
        comment="The directory this project is, as an absolute local path.",
    )
    # No `PathString` here: pydantic serializes a `Path` to its string natively, so the
    # engine's JSON serializer stores text, and the transmuter validates it back.
    extra_roots: Mapped[list[Path]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="Further directories that are also this project, as absolute paths.",
    )
    description: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        comment="What this project is.",
    )
    permission_mode: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="default",
        comment="Approval mode a conversation in this project starts under.",
    )
