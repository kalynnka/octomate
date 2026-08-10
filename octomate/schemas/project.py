from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Self

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)
from uuid_utils.compat import uuid7

from octomate.models.project import Project as ProjectModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.types.conversations import ConversationPermissionMode
from octomate.types.projects import ProjectOrigin


def local_path(value: str | Path) -> Path:
    """A project root, normalized: an absolute local path with a name to go by.

    `~` is expanded here for the reason `ConfigPath` exists: pydantic keeps `~/...`
    literal, and `Path("~/x").resolve()` yields ``<cwd>/~/x`` — a root like that
    matches nothing, silently. A relative root is made absolute for the same reason,
    since it would otherwise hang off whatever directory Octomate was started in.

    A url is refused rather than mangled: `Path("file:///srv/x")` is a perfectly real
    relative path, and it matches nothing. That check runs before the `Path` is built
    because `PurePath` collapses `://` to `:/`, and the scheme stops being visible.

    Having a name is the one thing a root needs that a directory does not, so that
    everything downstream gets a directory it can call the project by.

    The directory is deliberately not required to exist. A project is discovered from
    the sessions that ran in it, so a row outlives the directory it was discovered at,
    and this type is what rehydrates every row: refusing a dead path here would make
    one deleted checkout enough to stop the registry loading at all.
    """
    if "://" in str(value):
        raise ValueError(f"{value!r} is a url; a root is a plain local path")
    path = Path(value).expanduser().absolute()
    if not path.name:
        raise ValueError(f"{path} is the filesystem root, not a project")
    return path


# Symlinks are left alone — the manager resolves both sides when it compares.
LocalPath = Annotated[Path, BeforeValidator(local_path)]


@sqlalchemy_materia.bless(ProjectModel)
class Project(BaseTransmuter):
    """A project: the roots that are it, and how it is driven.

    A project exists because work happened in it — a Codex session whose workspace names
    a directory no project claims registers one. Nothing declares a project ahead of the
    first session that runs in one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    name: str = Field(
        default="",
        description=(
            "Stable name for this project. Defaults to the root's directory name — "
            "`~/Projects/octoverse/inky` is `inky` — and steps aside to `<name>-2` "
            "when another root already answers to it."
        ),
    )
    origin: ProjectOrigin = Field(
        default="declared",
        description=(
            "What registered this project: `declared` for one registered directly, "
            "or the native runtime whose session was found running in it."
        ),
    )

    root: LocalPath = Field(
        description="The directory this project is, as an absolute local path."
    )
    extra_roots: list[LocalPath] = Field(
        default_factory=list,
        description=(
            "Further directories that are also this project — a settings tree, a "
            "sibling checkout — each an absolute local path."
        ),
    )
    description: str | None = Field(
        default=None,
        description=(
            "What this project is, for a human reading the registry and for a model "
            "choosing between registered projects. Not agent instructions: those live "
            "in the project's own `AGENTS.md`/`CLAUDE.md`."
        ),
    )
    permission_mode: ConversationPermissionMode = Field(
        default="default",
        description=(
            "Approval mode a conversation in this project starts under; the "
            "conversation owns it afterwards."
        ),
    )

    @model_validator(mode="after")
    def name_from_root(self) -> Self:
        """A project with no name of its own is called after its root's directory.

        `local_path` has already refused a root with no name, so there is always one
        to take. A name the registry is already using is stepped around by
        `ProjectManager.available_name`, which is where the names in use are known.
        """
        self.name = self.name or self.root.name
        return self

    @property
    def roots(self) -> tuple[Path, ...]:
        """Every directory that is this project: `root`, then any `extra_roots`."""
        return (self.root, *self.extra_roots)

    def contains(self, path: Path) -> bool:
        """Whether `path` is one of this project's roots, or inside one.

        Both sides are resolved, because `is_relative_to` is lexical: a `..` and a
        symlink are the whole attack surface of a check that compares the string it
        was handed. A path that does not exist resolves fine, which is what a write
        creating a file needs.
        """
        resolved = path.resolve()
        return any(resolved.is_relative_to(root.resolve()) for root in self.roots)
