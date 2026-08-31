from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from octomate.config.mirrors import MirrorsConfig
from octomate.managers.workspaces.dependencies import install
from octomate.schemas.project import Project, RemoteUpstream

logger = logging.getLogger(__name__)

# Network git must fail fast rather than wait on stdin: a passphrase or password
# prompt with no terminal leaves a fetch looking hung instead of failed.
BATCH_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
}

# The empty repository every thread in no project forks from. Dot-prefixed to keep
# it out of the way of the project mirrors it sits among, which are named after
# projects and so after directories people actually have.
BLANK_MIRROR = ".blank"


class GitCommandError(RuntimeError):
    """A git command exited nonzero; the message carries its stderr."""


async def run_git(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    """Run one git command and answer its stdout, raising `GitCommandError` with
    the command and its stderr when it exits nonzero."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env={**os.environ, **env} if env else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise GitCommandError(
            f"git {' '.join(args)} failed: {stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace")


class MirrorManager:
    """Every project's mirror: a pristine git checkout at ``mirrors_dir/<project>``,
    made from the project's upstream and kept current by syncing from it.

    A remote upstream is cloned and freshened by fetching; a directory upstream is
    `git init`'d and freshened by staging the folder's state and committing when
    anything changed — the same job with a directory in place of a URL, so the
    default branch becomes a history of the folder. The folder stays authoritative:
    carrying reviewed work back out to it is a deliberate step, not part of syncing.

    A mirror stays on its default branch and no run ever writes to it; workspaces
    fork from it and are disposable.
    """

    def __init__(
        self,
        config: MirrorsConfig | None = None,
        mirrors_dir: Path = Path(".octomate/mirrors"),
    ) -> None:
        self.config = config if config is not None else MirrorsConfig()
        self.mirrors_dir = mirrors_dir
        self.locks: dict[str, asyncio.Lock] = {}
        self.synced: dict[str, float] = {}

    def path(self, project: Project | None = None) -> Path:
        """Where `project`'s mirror lives — or, with no project, where the blank one
        does: the mirror of nothing, which every thread in no project forks.

        Absolute, because directory sync points `GIT_DIR` here while running in the
        upstream folder, where a relative path would resolve against the wrong tree.
        """
        return (self.mirrors_dir / self.name(project)).resolve()

    def name(self, project: Project | None) -> str:
        """What this mirror is filed under, which is also what its lock and its
        freshness stamp are keyed by. The blank mirror is dot-prefixed, to keep it
        out of the way of the mirrors it sits among — those are named after
        projects, and so after directories people actually have."""
        return project.name if project is not None else BLANK_MIRROR

    async def sync(self, project: Project) -> Path:
        """The mirror, current enough to fork from: created from the upstream when
        missing, freshened from it when the freshness window has lapsed.

        Serialized per mirror, so a burst of materializations costs one sync: the
        making of it waits inside `create`, and the freshening of it waits here, on
        the same lock. Which is why `create` is called before this takes it —
        `asyncio.Lock` is not reentrant, and both halves are the same mirror's turn.

        A freshen that fails degrades to the stale mirror with a warning rather than
        failing the caller — work continues on what the machine already has, and
        the stale stamp is not renewed, so the next sync tries again. A mirror
        that cannot be created is a hard failure: there is nothing to fall back to.

        A mirror whose tree just changed is installed as well, which is what makes
        forking it cheap: the machine's package store is warm and the installed
        trees themselves are copied. Not on the window's short path, and not on the
        stale one — nothing moved there, so there is nothing to install.
        """
        # Asked before `create`, which is where the waiting happens: a mirror this
        # call had to make is one nothing can be behind, so it is not fetched again
        # for the sync that made it.
        missing = not self.path(project).is_dir()
        path = await self.create(project)
        async with self.locks.setdefault(project.name, asyncio.Lock()):
            if missing:
                self.synced[project.name] = time.monotonic()
                await install(path)
                return path
            last = self.synced.get(project.name)
            window = self.config.freshness_window
            if last is not None and time.monotonic() - last < window:
                return path
            try:
                await self.freshen(project, path)
            except (GitCommandError, OSError) as error:
                logger.warning("mirror for %s is stale: %s", project.name, error)
                return path
            self.synced[project.name] = time.monotonic()
            await install(path)
        return path

    async def create(self, project: Project | None = None) -> Path:
        """The mirror, made where there is none, and where it is.

        Three shapes, and a project's upstream picks between the first two: a remote
        is cloned onto its default branch, and a directory is `git init`'d with the
        folder's contents committed. With no project at all it is the blank mirror,
        which is a `git init` and one empty commit — that commit being what makes it
        forkable rather than special, since a repository with no commit clones onto
        an unborn branch, where git refuses the ordinary things a run does with one.
        Nothing binds to it and it takes no registry row: a thread that forks it is
        exactly a thread that is in no project.

        Serialized per mirror and idempotent, which is what the blank one needs —
        two threads in no project starting at once both ask for it, and a mirror
        half-made is a mirror something would fork. `sync` holds the same lock for
        the freshening half, and takes it after this rather than around it.

        A partial mirror is worse than none — it would read as existing and turn
        every later sync into a stale-mirror warning — so a creation that fails or
        is cancelled removes what it left before the error travels on.
        """
        path = self.path(project)
        async with self.locks.setdefault(self.name(project), asyncio.Lock()):
            if path.is_dir():
                return path
            self.mirrors_dir.mkdir(parents=True, exist_ok=True)
            upstream = project.upstream if project is not None else None
            try:
                if isinstance(upstream, RemoteUpstream):
                    await run_git("clone", upstream.url, str(path), env=BATCH_GIT_ENV)
                    return path
                await run_git("init", "-b", "main", str(path))
                if upstream is None:
                    await run_git(
                        *self.config.identity.commit_flags,
                        "commit",
                        "--allow-empty",
                        "-m",
                        "empty",
                        cwd=path,
                    )
                else:
                    await self.commit_folder(upstream.path, path)
            except BaseException:
                shutil.rmtree(path, ignore_errors=True)
                raise
        return path

    async def freshen(self, project: Project, path: Path) -> None:
        """Bring an existing mirror up to its upstream: fetch and move the default
        branch for a remote, stage-and-commit the folder for a directory.

        `--prune` follows upstream branch deletions; thread refs live outside the
        fetched refspec, so pruning never touches them. The hard reset is what a
        mirror allows that a working checkout would not: nothing else writes
        here, so the default branch simply follows upstream, force-pushes included.
        """
        upstream = project.upstream
        if isinstance(upstream, RemoteUpstream):
            await run_git("fetch", "--prune", "origin", cwd=path, env=BATCH_GIT_ENV)
            await run_git("reset", "--hard", "@{upstream}", cwd=path)
        else:
            await self.commit_folder(upstream.path, path)

    async def commit_folder(self, folder: Path, path: Path) -> None:
        """One sync of a directory upstream: stage exactly the folder's state into
        the mirror at `path`, and commit only when anything changed.

        Git's own index does the comparison. With `GIT_WORK_TREE` pointing at the
        folder, `add -A` stages additions, edits, and deletions in one pass while
        honoring the folder's own `.gitignore` — which is what keeps a served
        checkout's `.octomate/` out of its mirror — and no `.git` is ever created
        in the folder itself. The final reset materializes the commit into the
        mirror's tree; it never `git clean`s, so untracked state the mirror keeps
        (a warm `node_modules`, say) survives every sync.
        """
        against = {
            "GIT_DIR": str(path / ".git"),
            "GIT_WORK_TREE": str(folder),
            # A scratch index, so the mirror's own index keeps tracking its HEAD.
            # Staging a deletion in the shared index would untrack the file, and
            # the final reset — which deletes what is tracked-but-gone and spares
            # what was never tracked — would then leave it behind in the mirror.
            "GIT_INDEX_FILE": str(path / ".git" / "sync-index"),
        }
        await run_git("add", "-A", cwd=folder, env=against)
        # `status` rather than `diff HEAD`, because a just-init'd mirror has no
        # HEAD to diff against, and an unchanged folder has to read as clean.
        if not await run_git("status", "--porcelain", cwd=folder, env=against):
            return
        await run_git(
            *self.config.identity.commit_flags,
            "commit",
            "-m",
            f"sync {folder}",
            cwd=folder,
            env=against,
        )
        await run_git("reset", "--hard", cwd=path)

    async def reconcile(self, projects: list[Project]) -> None:
        """Sync every enabled project's mirror, as startup runs it over the
        registry: registering a project is what grants it a mirror, so the
        mirrors directory agrees with the registry before the first
        materialization asks.

        One unreachable upstream must not stop the rest — each failure is logged
        and the sweep continues, the same isolation tentacle startup gets.
        """
        for project in projects:
            if not project.enabled:
                continue
            try:
                await self.sync(project)
            except Exception:
                logger.exception("mirror for %s could not be made", project.name)
