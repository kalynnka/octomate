from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from octomate.managers.mirrors import run_git

logger = logging.getLogger(__name__)

# `cp` forks a file by cloning its extents instead of copying its bytes, with `-c`
# on BSD and `--reflink=always` on GNU. Each flag is an error on the other's `cp`,
# and neither says anything about the filesystem underneath, so which one works is
# probed rather than dispatched on by platform.
REFLINK_FLAGS = ("-c", "--reflink=always")


class CopyError(RuntimeError):
    """A `cp` exited nonzero; the message carries its stderr."""


async def copy(source: Path, target: Path, flag: str) -> None:
    """Copy `source` to `target` with `flag`, raising `CopyError` with the stderr
    when `cp` exits nonzero.

    `-a` because a fork has to be the tree it was made from: a symlink stays a
    symlink, and preserved timestamps keep the copied git index valid, so the
    workspace's first `git status` compares stat data rather than rehashing
    every file in the tree.
    """
    process = await asyncio.create_subprocess_exec(
        "cp",
        "-a",
        flag,
        str(source),
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise CopyError(
            f"cp -a {flag} {source} {target} failed: "
            f"{stderr.decode(errors='replace').strip()}"
        )


class WorkspaceManager:
    """Every project-bound thread's workspace: a fork of the project's mirror at
    ``workspaces_dir/<thread_id>``, checked out on the thread's own branch and
    released when the disk is wanted back.

    A fork is a complete, independent repository — its own `.git`, its own object
    store — made by cloning the mirror's extents where the filesystem does
    copy-on-write and by `git clone` where it does not. Never a `git worktree`,
    whose `.git` is a file pointing back at the mirror: commits in one write
    outside the workspace, which an agent's write sandbox refuses.

    Which mirror to fork is the caller's to say — `MirrorManager.sync` is what
    produces one — so nothing here knows about projects or registries.
    """

    def __init__(self, workspaces_dir: Path = Path(".octomate/workspaces")) -> None:
        self.workspaces_dir = workspaces_dir
        self.reflink: str | None = None
        self.probed = False
        self.locks: dict[uuid.UUID, asyncio.Lock] = {}

    def path(self, thread_id: uuid.UUID) -> Path:
        """Where this thread's workspace lives. Absolute, because it becomes the
        working directory of a process Octomate did not start in its own."""
        return (self.workspaces_dir / str(thread_id)).resolve()

    async def detect(self) -> str | None:
        """The `cp` flag that forks without copying bytes on this host, or None
        where nothing does and workspaces are cloned instead.

        Probed once, by actually cloning a file where workspaces land: the answer
        belongs to that filesystem rather than to the platform, and it cannot
        change under a running host. A host with no copy-on-write filesystem is
        not a failure — it pays a full tree per workspace and works.
        """
        if self.probed:
            return self.reflink
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(dir=self.workspaces_dir))
        try:
            source = probe / "source"
            source.write_bytes(b"probe")
            for flag in REFLINK_FLAGS:
                try:
                    await copy(source, probe / "target", flag)
                except CopyError:
                    continue
                self.reflink = flag
                break
        finally:
            shutil.rmtree(probe)
        self.probed = True
        logger.info(
            "thread workspaces fork by %s",
            f"cp -a {self.reflink}" if self.reflink else "git clone",
        )
        return self.reflink

    async def materialize(self, thread_id: uuid.UUID, mirror: Path) -> Path:
        """This thread's workspace: a fork of `mirror`, on the thread's branch.

        A workspace that already exists is the thread's live tree, uncommitted
        work included, and is answered as it stands — forking again would throw
        that work away. Serialized per thread, so two turns arriving together
        materialize once.

        A fork that fails is removed before the error travels on: half a
        workspace would read as that live tree ever after.
        """
        path = self.path(thread_id)
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            if path.is_dir():
                return path
            self.workspaces_dir.mkdir(parents=True, exist_ok=True)
            flag = await self.detect()
            try:
                if flag is not None:
                    await copy(mirror, path, flag)
                else:
                    await run_git("clone", str(mirror), str(path))
                await run_git(
                    "checkout", "-b", f"octomate/thread-{thread_id}", cwd=path
                )
            except BaseException:
                shutil.rmtree(path, ignore_errors=True)
                raise
        return path

    async def release(self, thread_id: uuid.UUID) -> None:
        """Give this thread's workspace back to the disk, or nothing when it is
        already gone — pruning and releasing a thread that never had one both
        arrive here, and a workspace is a cache either way.

        The mirror is untouched: a copied fork owns its objects outright, and a
        cloned one holds hard links, where unlinking a link leaves the file. In
        a thread, off the loop: a released tree is as big as the project.
        """
        path = self.path(thread_id)
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            if path.is_dir():
                await asyncio.to_thread(shutil.rmtree, path)
