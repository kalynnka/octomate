from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Self

from arcanus import BaseTransmuter
from arcanus.base import Identity
from pydantic import BeforeValidator, ConfigDict, Field, model_validator
from uuid_utils.compat import uuid7

from octomate.models.project import Project as ProjectModel
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationPermissionMode


def local_path(value: str | Path) -> Path:
    """A project root: an absolute path to a named directory that is there.

    `~` is expanded here for the reason `ConfigPath` exists: pydantic keeps `~/...`
    literal, and `Path("~/x").resolve()` yields ``<cwd>/~/x`` — a root like that
    matches nothing, silently. A relative root is made absolute for the same reason,
    since it would otherwise hang off whatever directory Octomate was started in.

    A url is refused rather than mangled: `Path("file:///srv/x")` is a perfectly real
    relative path, and it matches nothing. That check runs before the `Path` is built
    because `PurePath` collapses `://` to `:/`, and the scheme stops being visible.

    The rest is what makes a root a root. Declaring a project is a claim about this
    machine, so a root that is missing or is a file fails at config load, where an
    operator is looking, rather than at the first cwd that should have matched it.
    Everything downstream gets a directory with a name it can be called by.
    """
    if "://" in str(value):
        raise ValueError(f"{value!r} is a url; a root is a plain local path")
    path = Path(value).expanduser().absolute()
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    if not path.is_dir():
        raise ValueError(f"{path} is a file; a root is a directory")
    if not path.name:
        raise ValueError(f"{path} is the filesystem root, not a project")
    return path


# Symlinks are left alone — the manager resolves both sides when it compares.
LocalPath = Annotated[Path, BeforeValidator(local_path)]


@sqlalchemy_materia.bless(ProjectModel)
class Project(BaseTransmuter):
    """A YAML-declared project: the roots that are it, and how it is driven."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    name: str = Field(
        default="",
        description=(
            "Stable name for this project. Defaults to the root's directory name — "
            "`~/Projects/octoverse/inky` is `inky` — so an entry only names itself "
            "when the directory is not what it should be called."
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
            "choosing between declared projects. Not agent instructions: those live "
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
        to take. This runs here rather than on `Create`, which is what the config
        validates: arcanus generates that model and it inherits no validators, so a
        name derived there would have to be derived twice.
        """
        self.name = self.name or self.root.name
        return self
