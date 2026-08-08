from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, ValidateAs
from pydantic_core import PydanticCustomError

from octomate.schemas.project import Project


def existing_roots(project: Project) -> Project:
    """Declaring a project is a claim about this machine: every root must be a
    directory that is there, checked at config load, where the operator who wrote
    the entry is looking. The schema deliberately does not check this — it also
    validates rows rehydrated from the database, which may outlive the directories
    they named — so the config boundary is where the claim is enforced.
    """
    for kind, path in (
        ("root", project.root),
        *(("extra root", extra) for extra in project.extra_roots),
    ):
        if not path.exists():
            raise PydanticCustomError(
                "project_root_missing",
                "project '{name}': {kind} {path} does not exist on this machine",
                {"name": project.name, "kind": kind, "path": str(path)},
            )
        if not path.is_dir():
            raise PydanticCustomError(
                "project_root_not_a_directory",
                "project '{name}': {kind} {path} is a file; a root is a directory",
                {"name": project.name, "kind": kind, "path": str(path)},
            )
    return project


# `Create` rather than `Project` itself, so a YAML entry cannot mint an `id`: the
# generated model has no such field and drops one silently, where the full model
# would accept it. `shell` builds the concrete project from the validated partial.
ConfigProject = Annotated[
    Project,
    ValidateAs(Project.Create, Project.shell),
    AfterValidator(existing_roots),
]
