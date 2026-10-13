from __future__ import annotations

from octomate.schemas.project import Project

# The `projects:` block: declared code locations keyed by project name. A bare
# mapping rather than a model, because a project's own shape is `Project.Create`
# and the block adds nothing around it — named here so the registry that reads it
# and the config that declares it agree on one type.
type ProjectsConfig = dict[str, Project.Create]
