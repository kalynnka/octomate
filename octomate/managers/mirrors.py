from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from octomate.config.mirrors import MirrorsConfig
from octomate.schemas.project import Project, RemoteUpstream

logger = logging.getLogger(__name__)

# Network git must fail fast rather than wait on stdin: a passphrase or password
# prompt with no terminal leaves a fetch looking hung instead of failed.
BATCH_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
}


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
        # The `-c` flags on every commit this manager makes: the configured
        # identity — per invocation, so nothing leaks into the mirror's own
        # config — and signing off, since a sync commit is the machine's record
        # of a folder and nothing a host's signing key should vouch for.
        identity = self.config.identity
        self.commit_flags = (
            "-c",
            f"user.name={identity.name}",
            "-c",
            f"user.email={identity.email}",
            "-c",
            "commit.gpgsign=false",
        )
        self.locks: dict[str, asyncio.Lock] = {}
        self.synced: dict[str, float] = {}

    def path(self, project: Project) -> Path:
        """Where `project`'s mirror lives. Absolute, because directory sync points
        `GIT_DIR` here while running in the upstream folder, where a relative path
        would resolve against the wrong tree."""
        return (self.mirrors_dir / project.name).resolve()

    async def sync(self, project: Project) -> Path:
        """The mirror, current enough to fork from: created from the upstream when
        missing, freshened from it when the freshness window has lapsed.

        Serialized per mirror, so a burst of materializations costs one sync. A
        freshen that fails degrades to the stale mirror with a warning rather than
        failing the caller — work continues on what the machine already has, and
        the stale stamp is not renewed, so the next sync tries again. A mirror
        that cannot be created is a hard failure: there is nothing to fall back to.
        """
        path = self.path(project)
        async with self.locks.setdefault(project.name, asyncio.Lock()):
            last = self.synced.get(project.name)
            window = self.config.freshness_window
            if last is not None and time.monotonic() - last < window:
                return path
            if not path.is_dir():
                await self.create(project, path)
            else:
                try:
                    await self.freshen(project, path)
                except (GitCommandError, OSError) as error:
                    logger.warning("mirror for %s is stale: %s", project.name, error)
                    return path
            self.synced[project.name] = time.monotonic()
        return path

    async def create(self, project: Project, path: Path) -> None:
        """Make the mirror: clone a remote upstream onto its default branch, or
        `git init` a directory upstream's and commit the folder's contents.

        A partial mirror is worse than none — it would read as existing and turn
        every later sync into a stale-mirror warning — so a creation that fails or
        is cancelled removes what it left before the error travels on.
        """
        self.mirrors_dir.mkdir(parents=True, exist_ok=True)
        try:
            upstream = project.upstream
            if isinstance(upstream, RemoteUpstream):
                await run_git("clone", upstream.url, str(path), env=BATCH_GIT_ENV)
            else:
                await run_git("init", "-b", "main", str(path))
                await self.commit_folder(upstream.path, path)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise

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
            *self.commit_flags,
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
