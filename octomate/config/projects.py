from __future__ import annotations

from typing import Annotated

from pydantic import ValidateAs

from octomate.schemas.project import Project

# A declared project is a `Project`, validated through its own create partial rather
# than through a config model restating it — the registry stores exactly these fields,
# and `name` comes from the `projects:` key.
ConfigProject = Annotated[Project, ValidateAs(Project.Create, Project.shell)]
