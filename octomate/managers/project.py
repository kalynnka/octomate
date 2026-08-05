from __future__ import annotations

from pathlib import Path

from octomate.database import async_session
from octomate.schemas.project import Project

RECONCILED_FIELDS = {"root", "extra_roots", "description", "permission_mode"}


class ProjectManager:
    """The project registry, currently configured through YAML.

    A project exists because ``projects:`` declares it; nothing creates one
    implicitly, and a cwd under no declared root resolves to nothing, which is a
    normal outcome rather than an error.

    Resolution reads the persisted rows rather than the config, so the registry is
    one thing whatever registered it. Projects are expected to remain a small
    registry and are all cached.

    Note the name collision: ``projects:`` here is the operator's registry of code
    locations, and is unrelated to ``~/.claude/projects/`` (``CLAUDE_PROJECTS_DIRS``
    in ``tentacles/agents/claude/transcript.py``), which is transcript storage.
    """

    def __init__(self, config: list[Project] | None = None) -> None:
        self.config = config or []
        self.projects: dict[str, Project] = {}
        # (resolved root, project name), deepest first, so the first hit is the
        # longest prefix and a monorepo plus a sub-package can both be projects.
        self.roots: list[tuple[Path, str]] = []

    def get(self, name: str) -> Project | None:
        """The project called ``name``, or None if none is registered as it.

        Reads the cache `reconcile` fills, so it does no IO and is safe from a sync
        caller. A name that was declared once and has since been removed is not here:
        the row is retained, but the registry is what YAML currently declares.
        """
        return self.projects.get(name)

    def list(self) -> list[Project]:
        """Every registered project, in the order it was declared."""
        return list(self.projects.values())

    def resolve(self, cwd: Path) -> str | None:
        """The name of the declared project ``cwd`` is in, or None for none.

        Resolved before comparing, because `is_relative_to` is lexical: `/tmp` against
        a declared `/private/tmp`, or a symlinked home, would otherwise miss silently.
        """
        resolved = cwd.resolve()
        for root, name in self.roots:
            if resolved.is_relative_to(root):
                return name
        return None

    async def reconcile(self) -> None:
        """Make persisted projects and the resolution index match YAML.

        A project's name is its stable identity, so two declarations cannot share
        one — the list makes that possible where the old mapping made it impossible,
        and two roots whose directories are named alike is the easy way in. Projects
        absent from YAML are retained for future registration sources, but drop out
        of the index: a declaration is what makes a cwd resolve, so removing one
        stops it resolving without discarding the row history may already name.
        """
        declared: dict[Path, str] = {}
        config_by_name: dict[str, Project] = {}
        for config_project in self.config:
            name = config_project.name
            if name in config_by_name:
                raise ValueError(f"{name!r} is declared twice")
            config_by_name[name] = config_project
            for root in (config_project.root, *config_project.extra_roots):
                resolved = root.resolve()
                if (claimed_by := declared.get(resolved)) is not None:
                    raise ValueError(
                        f"{resolved} is declared for both {claimed_by!r} and {name!r}"
                    )
                declared[resolved] = name

        async with async_session() as session:
            stored = list(await session.list(Project, limit=None))
            projects_by_name = {project.name: project for project in stored}

            for name, config_project in config_by_name.items():
                project = projects_by_name.get(name)
                if project is None:
                    project = Project(
                        name=name,
                        **config_project.model_dump(include=RECONCILED_FIELDS),
                    )
                    session.add(project)
                    projects_by_name[name] = project
                else:
                    for field in RECONCILED_FIELDS:
                        setattr(project, field, getattr(config_project, field))

            await session.commit()

        self.projects = {name: projects_by_name[name] for name in config_by_name}
        self.roots = sorted(
            declared.items(),
            key=lambda pair: len(pair[0].parts),
            reverse=True,
        )
