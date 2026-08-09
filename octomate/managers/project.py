from __future__ import annotations

import asyncio
from collections.abc import Iterable
from itertools import count
from pathlib import Path

from octomate.database import async_session
from octomate.schemas.project import Project, local_path
from octomate.types.projects import ProjectOrigin


class ProjectManager:
    """The project registry: every code location Octomate knows by name.

    A project exists because work happened in it. A native session running in a
    directory nothing claims registers one (`ensure`), so the registry describes where
    this machine is actually worked in rather than what an operator remembered to write
    down. Nothing declares a project ahead of the first session that runs in one.

    Every registered project resolves, however it got there. Projects are expected to
    remain a small registry and are all cached.

    Note the name collision: ``projects:`` here is the operator's registry of code
    locations, and is unrelated to ``~/.claude/projects/`` (``CLAUDE_PROJECTS_DIRS``
    in ``tentacles/agents/claude/transcript.py``), which is transcript storage.
    """

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        # (resolved root, project name), deepest first, so the first hit is the
        # longest prefix and a monorepo plus a sub-package can both be projects.
        self.roots: list[tuple[Path, str]] = []
        # Serializes first sightings, as the conversation cache does: two sessions
        # starting at once in one undeclared directory must not both register it.
        self.ensure_lock = asyncio.Lock()

    def get(self, name: str) -> Project | None:
        """The project called ``name``, or None if none is registered as it.

        Reads the cache the registry keeps, so it does no IO and is safe from a sync
        caller.
        """
        return self.projects.get(name)

    def list(self) -> list[Project]:
        """Every registered project."""
        return list(self.projects.values())

    def resolve(self, cwd: Path) -> str | None:
        """The name of the project ``cwd`` is in, or None if none holds it yet.

        Resolved before comparing, because `is_relative_to` is lexical: `/tmp` against
        a registered `/private/tmp`, or a symlinked home, would otherwise miss silently.
        """
        resolved = cwd.resolve()
        for root, name in self.roots:
            if resolved.is_relative_to(root):
                return name
        return None

    async def load(self) -> None:
        """Read every stored project into the cache and the resolution index.

        One pass at startup, because a project resolves by existing: nothing narrows
        the registry to a subset that some other source still vouches for.
        """
        async with async_session() as session:
            self.index(await session.list(Project, limit=None))

    async def ensure(self, cwd: Path, *, origin: ProjectOrigin) -> Project:
        """The project `cwd` is in, registering one rooted there if none claims it yet.

        This is how a project comes to exist: a native session runs somewhere, and that
        directory becomes a project because work happened in it. Nothing has to have
        been declared first.

        A cwd *inside* a project that is already registered returns that project rather
        than registering a second one under it — the run is simply somewhere below its
        project's root, which is where runs are allowed to be. Without that, every
        package of a monorepo would become a project of its own the first time someone
        opened a session in it.
        """
        name = self.resolve(cwd)
        if name is not None:
            return self.projects[name]
        async with self.ensure_lock:
            # The loser of a race re-checks under the lock and becomes a hit, rather
            # than a second row at one root.
            name = self.resolve(cwd)
            if name is not None:
                return self.projects[name]
            root = local_path(cwd)
            project = Project(
                root=root, name=self.available_name(root.name), origin=origin
            )
            async with async_session() as session:
                session.add(project)
                await session.commit()
            self.index([*self.projects.values(), project])
            return project

    def available_name(self, wanted: str) -> str:
        """`wanted` if the registry does not already use it, else the same name with a
        counter — `api`, then `api-2`. Two directories really can be called the same
        thing (`~/Projects/api` and `~/Projects/vita/api` are both here), and a name is
        a label rather than the identity, so a collision is renamed rather than refused.

        Assigned once and never revised: a name that history already refers to has to
        keep meaning what it did.
        """
        if wanted not in self.projects:
            return wanted
        return next(
            candidate
            for suffix in count(2)
            if (candidate := f"{wanted}-{suffix}") not in self.projects
        )

    def index(self, projects: Iterable[Project]) -> None:
        """Rebuild the by-name map and the resolution index from `projects`.

        Roots are sorted deepest first, so the first hit is the longest prefix and a
        monorepo plus a package inside it can both be projects.
        """
        self.projects = {project.name: project for project in projects}
        self.roots = sorted(
            (
                (root.resolve(), project.name)
                for project in self.projects.values()
                for root in (project.root, *project.extra_roots)
            ),
            key=lambda pair: len(pair[0].parts),
            reverse=True,
        )
