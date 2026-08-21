from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from octomate.database import async_session
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread


class ProjectManager:
    """The project registry: every code location Octomate knows by name.

    A project gets here one way: the operator declares it in the ``projects:`` block,
    which `reconcile` upserts at startup. Declaring is a trust act — a declared
    project's `AGENTS.md` reaches an agent as instructions — so a session running
    where nothing is declared files its work under no project rather than minting one.

    Every enabled project resolves. Projects are expected to remain a small registry
    and are all cached.

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

    def get(self, name: str) -> Project | None:
        """The project called ``name``, or None if none is registered as it. Disabled
        ones answer too — a thread filed under one still has to say where it was.

        Reads the cache the registry keeps, so it does no IO and is safe from a sync
        caller.
        """
        return self.projects.get(name)

    async def of(self, thread: Thread) -> Project | None:
        """The project this thread's work runs in, or None when it runs in none.

        The thread is where a project is bound, so its attribution is the question;
        whether that attribution still names somewhere an agent may run is this
        registry's answer, and the two are not the same. A thread naming a project
        the block no longer declares is in none, and so is one whose project is
        disabled — the row is retained either way, because it still has to say where
        the work was filed, but neither has a root left to offer.

        Read through the registry rather than off the row, which is exactly why this
        is not just `thread.project`.
        """
        attributed = await thread.project
        if attributed is None:
            return None
        project = self.get(attributed.name)
        return project if project is not None and project.enabled else None

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
        unique column and the identity, so declaring a directory that already has a
        row — left by an earlier block, or by the era when sessions registered
        projects — adopts that row under the declared name rather than colliding with
        it on the way to a second row for one directory.

        A row the block no longer declares is left exactly as it is. The threads and
        runs recorded under it still name it, so dropping a declaration is not a
        request to forget the history filed there.

        What does get cleared is `enabled`, for every row whose root is no longer a
        directory on disk — a deleted checkout, an unmounted drive. The row stays, so
        the threads and runs that name it still read back, and it re-enables by itself
        the moment the directory is there again.

        Both conflict checks run over the reconciled registry rather than over the
        declared block, because a declaration collides with a row an earlier block
        left behind as readily as with another declaration, and the operator reading
        the error needs to be told which two.
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
