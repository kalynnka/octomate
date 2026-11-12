"""Capture writable trees as data in a temporary, host-owned Git repository."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from octomate.config.mirrors import GitIdentity
from octomate.managers.workspaces.mirrors import GitCommandError, run_git

SNAPSHOT_PREFIX = "octomate: "


@contextmanager
def directory(path: Path, *, parent: int | None = None) -> Generator[int]:
    """Open each directory component without following a replaced symlink."""
    if ".." in path.parts or (parent is not None and path.is_absolute()):
        raise GitCommandError("Snapshot paths must stay inside the workspace")
    with ExitStack() as opened:
        descriptor = (
            os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            if parent is None
            else os.dup(parent)
        )
        opened.callback(os.close, descriptor)
        parts = path.parts[1:] if parent is None else path.parts
        for part in parts:
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            opened.callback(os.close, descriptor)
        yield descriptor


def copy_entry(source: int, name: str, target: Path, *, symlinks: bool) -> None:
    """Copy bytes through non-following descriptors; metadata may not contain links."""
    mode = os.stat(name, dir_fd=source, follow_symlinks=False).st_mode
    if stat.S_ISLNK(mode):
        if not symlinks:
            raise GitCommandError("Git metadata cannot contain symbolic links")
        target.symlink_to(os.readlink(name, dir_fd=source))
        return
    if stat.S_ISDIR(mode):
        target.mkdir(exist_ok=True)
        with directory(Path(name), parent=source) as child:
            for entry in os.listdir(child):
                copy_entry(child, entry, target / entry, symlinks=symlinks)
        return
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source
    )
    with os.fdopen(descriptor, "rb") as origin:
        info = os.fstat(origin.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise GitCommandError("Snapshots require regular files without hardlinks")
        with target.open("wb") as destination:
            shutil.copyfileobj(origin, destination)
    target.chmod(0o755 if info.st_mode & 0o111 else 0o644)


def copy_metadata(source: Path, target: Path) -> None:
    """Import only object/ref/index data, never configuration, hooks or alternates."""
    with directory(source) as root, directory(Path(".git"), parent=root) as git:
        for name in ("HEAD", "objects", "refs"):
            copy_entry(git, name, target / name, symlinks=False)
        for name in os.listdir(git):
            if name in {"index", "packed-refs"} or name.startswith("sharedindex."):
                copy_entry(git, name, target / name, symlinks=False)
        (target / "info").mkdir(exist_ok=True)
        try:
            with directory(Path("info"), parent=git) as info:
                copy_entry(info, "exclude", target / "info/exclude", symlinks=False)
        except FileNotFoundError:
            pass
    if any(
        (target / "objects/info" / name).exists()
        for name in ("alternates", "http-alternates")
    ):
        raise GitCommandError("Snapshots cannot use external Git object stores")
    if list((target / "objects/pack").glob("*.promisor")):
        raise GitCommandError("Snapshots require a complete Git object store")


def copy_files(source: Path, target: Path, names: list[str]) -> None:
    """Copy selected working files without dereferencing symlinks or traversals."""
    with directory(source) as root:
        for name in dict.fromkeys(names):
            copy_working_file(root, name, target)


def copy_working_file(source: int, name: str, target: Path) -> None:
    """Copy one indexed path, preserving tracked deletions and symlink contents."""
    path = Path(name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
    ):
        raise GitCommandError("Invalid working file path in the Git index")
    try:
        with directory(path.parent, parent=source) as parent:
            if stat.S_ISDIR(
                os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode
            ):
                raise GitCommandError("Embedded repositories cannot be snapshotted")
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            copy_entry(parent, path.name, target / path, symlinks=True)
    except FileNotFoundError:
        # A tracked deletion belongs in the snapshot as a deletion.
        return


@dataclass(frozen=True)
class Snapshot:
    path: Path  # Host-owned temporary repository, never attached to the habitat.
    commit: str  # Validated capture containing the working tree and its HEAD parent.

    @classmethod
    @asynccontextmanager
    async def capture(
        cls, source: Path, identity: GitIdentity
    ) -> AsyncGenerator[Snapshot]:
        with tempfile.TemporaryDirectory(prefix="octomate-snapshot-") as temporary:
            path = Path(temporary)
            await run_git("init", "--template=", str(path), isolated=True)
            await asyncio.to_thread(copy_metadata, source, path / ".git")
            await run_git(
                "fsck", "--full", "--strict", "--no-reflogs", cwd=path, isolated=True
            )
            head = (
                await run_git(
                    "rev-parse", "--verify", "HEAD^{commit}", cwd=path, isolated=True
                )
            ).strip()
            branch = (
                await run_git(
                    "rev-parse", "--abbrev-ref", "HEAD", cwd=path, isolated=True
                )
            ).strip()
            if not (path / ".git/index").exists():
                await run_git("read-tree", "HEAD", cwd=path, isolated=True)
            entries = await run_git(
                "ls-files", "--stage", "-z", cwd=path, isolated=True
            )
            if any(entry.startswith("160000 ") for entry in entries.split("\0")):
                raise GitCommandError("Submodule workspaces cannot be snapshotted")
            names = await run_git(
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                cwd=path,
                env={"GIT_WORK_TREE": str(source)},
                isolated=True,
            )
            await asyncio.to_thread(
                copy_files, source, path, [name for name in names.split("\0") if name]
            )
            await run_git("add", "-A", cwd=path, isolated=True)
            tree = (await run_git("write-tree", cwd=path, isolated=True)).strip()
            commit = (
                await run_git(
                    *identity.commit_flags,
                    "commit-tree",
                    tree,
                    "-p",
                    head,
                    "-m",
                    f"{SNAPSHOT_PREFIX}{branch}",
                    cwd=path,
                    isolated=True,
                )
            ).strip()
            yield cls(path, commit)

    async def save(self, mirror: Path, ref: str) -> None:
        """Import this capture only when the canonical record differs."""
        if await self.matches(mirror, ref):
            return
        await run_git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-auto-maintenance",
            str(self.path),
            f"+{self.commit}:{ref}",
            cwd=mirror,
            isolated=True,
        )

    async def matches(self, mirror: Path, ref: str) -> bool:
        """Compare against the host's canonical record, never a workspace marker."""
        if not await asyncio.to_thread(mirror.is_dir):
            return False
        if not await run_git(
            "for-each-ref", "--format=%(objectname)", ref, cwd=mirror, isolated=True
        ):
            return False
        state = ("show", "-s", "--format=%T %P %s")
        return await run_git(
            *state, self.commit, cwd=self.path, isolated=True
        ) == await run_git(*state, ref, cwd=mirror, isolated=True)
