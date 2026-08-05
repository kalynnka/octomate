from __future__ import annotations

from typing import Annotated

from pydantic import ValidateAs

from octomate.schemas.project import Project

# `Create` rather than `Project` itself, so a YAML entry cannot mint an `id`: the
# generated model has no such field and drops one silently, where the full model
# would accept it. `shell` builds the concrete project from the validated partial.
ConfigProject = Annotated[Project, ValidateAs(Project.Create, Project.shell)]
