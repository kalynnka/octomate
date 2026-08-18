from __future__ import annotations

import asyncio
from collections.abc import Iterable
from itertools import count
from pathlib import Path

from octomate.database import async_session
from octomate.schemas.project import DirectoryUpstream, Project, local_path
from octomate.types.projects import ProjectOrigin


class ProjectManager:
    """The project registry: every code location Octomate knows by name.

    A project gets here two ways. The operator declares one in the ``projects:`` block,
    which `reconcile` upserts at startup; or work happens in a directory nothing claims
    and a native session registers it (`ensure`). The first is a trust act — a declared
    project's `AGENTS.md` reaches an agent as instructions — and the second is a record
    of where this machine is actually worked in.

    Every enabled project resolves, however it got there. Projects are expected to
    remain a small registry and are all cached.

    Note the name collision: ``projects:`` here is the operator's registry of code
    locations, and is unrelated to ``~/.claude/projects/``, which is where Claude
    Code stores transcripts.
    """

    def __init__(self, config: dict[str, Project.Create] | None = None) -> None:
        self.config = config or {}
        self.projects: dict[str, Project] = {}
        # (resolved root, project name), deepest first, so the first hit is the
        # longest prefix and a monorepo plus a sub-package can both be projects.
        self.roots: list[tuple[Path, str]] = []
        # Serializes first sightings, as the conversation cache does: two sessions
        # starting at once in one undeclared directory must not both register it.
        self.ensure_lock = asyncio.Lock()

    def get(self, name: str) -> Project | None:
        """The project called ``name``, or None if none is registered as it. Disabled
        ones answer too — a thread filed under one still has to say where it was.

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

    async def reconcile(self) -> None:
        """Upsert the declared block into the registry, then index what disk still has.

        A declaration is matched to its row by `root`, not by name: the root is the
        unique column and the identity, so declaring a directory a native session
        already registered adopts that row — under the declared name — rather than
        colliding with it on the way to a second row for one directory.

        A row the block no longer declares is left exactly as it is. Declaring is not
        the only way a project gets here, so absence from YAML says nothing about a
        project a Codex session registered.

        What does get cleared is `enabled`, for every row whose root is no longer a
        directory on disk — a deleted checkout, an unmounted drive. The row stays, so
        the threads and runs that name it still read back, and it re-enables by itself
        the moment the directory is there again.

        Both conflict checks run over the reconciled registry rather than over the
        declared block, because a declaration collides with a project a session
        registered as readily as with another declaration, and the operator reading the
        error needs to be told which two.
        """
        async with async_session() as session:
            stored = list(await session.list(Project, limit=None))
            by_root = {project.root.resolve(): project for project in stored}
            for name, config in self.config.items():
                # Shelled here and not at config time: a transmuter validated outside
                # the materia carries no row, and this is the first place inside one.
                declared = Project.shell(config)
                declared.name = name
                project = by_root.get(declared.root.resolve())
                if project is None:
                    session.add(declared)
                    stored.append(declared)
                    continue
                project.name = name
                project.extra_roots = declared.extra_roots
                project.upstream = declared.upstream
                project.description = declared.description
            named: dict[str, Path] = {}
            rooted: dict[Path, str] = {}
            for project in stored:
                # Ahead of the unique constraint, which would refuse the same rename
                # without saying which two directories wanted the name.
                if (other := named.get(project.name)) is not None:
                    raise ValueError(
                        f"{other} and {project.root} are both named {project.name!r}"
                    )
                named[project.name] = project.root
                # Every root a project answers to, so an `extra_roots` entry pointing at
                # another project is refused too — nothing constrains those, and a
                # directory two projects claim resolves to whichever sorted first.
                for root in project.roots:
                    if (claimed_by := rooted.get(root.resolve())) is not None:
                        raise ValueError(
                            f"{root} is claimed by both {claimed_by!r} and "
                            f"{project.name!r}"
                        )
                    rooted[root.resolve()] = project.name
                project.enabled = project.root.is_dir()
            await session.commit()
            self.index(stored)

    async def ensure(self, cwd: Path, *, origin: ProjectOrigin) -> Project:
        """The project `cwd` is in, registering one rooted there if none claims it yet.

        This is how a project comes to exist: a Codex session names a directory as its
        workspace, and that directory becomes a project because work happened in it.
        Nothing has to have been declared first. Callers that only want to file work
        under a project already holding a directory use `resolve` — see `ProjectOrigin`
        for why a Claude session is one of them.

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
                root=root,
                name=self.available_name(root.name),
                origin=origin,
                # A discovered project's mirror syncs from the directory it was
                # discovered at — the only upstream a bare cwd can name.
                upstream=DirectoryUpstream(path=root),
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

        Every project is kept by name, disabled ones included, so a thread filed under
        one still reads back and its name stays taken. Only enabled projects get roots:
        a directory that is gone resolves nothing, and a run must not be sent there.

        Roots are sorted deepest first, so the first hit is the longest prefix and a
        monorepo plus a package inside it can both be projects.
        """
        self.projects = {project.name: project for project in projects}
        self.roots = sorted(
            (
                (root.resolve(), project.name)
                for project in self.projects.values()
                if project.enabled
                for root in (project.root, *project.extra_roots)
            ),
            key=lambda pair: len(pair[0].parts),
            reverse=True,
        )
