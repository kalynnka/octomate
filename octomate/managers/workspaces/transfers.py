"""Carry ignored files between writable trees without following redirected paths."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path

from octomate.managers.workspaces.mirrors import GitCommandError, run_git
from octomate.managers.workspaces.snapshots import copy_metadata, directory


async def copy_ignored(source: Path, target: Path) -> None:
    """Enumerate with host-owned Git metadata; copy files but not ignored folders."""
    with tempfile.TemporaryDirectory(prefix="octomate-carry-") as temporary:
        repository = Path(temporary)
        await run_git("init", "--template=", str(repository), isolated=True)
        await asyncio.to_thread(copy_metadata, source, repository / ".git")
        ignored = await run_git(
            "-c",
            f"core.excludesFile={os.devnull}",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
            cwd=repository,
            env={"GIT_WORK_TREE": str(source)},
            isolated=True,
        )
        names = [
            name for name in ignored.split("\0") if name and not name.endswith("/")
        ]
        await asyncio.to_thread(copy_ignored_files, source, target, names)


def copy_ignored_files(source: Path, target: Path, names: list[str]) -> None:
    with directory(source) as origin, directory(target) as destination:
        for name in names:
            try:
                copy_ignored_file(origin, destination, name)
            except FileExistsError:
                # Exclusive creation also preserves entries created during the copy.
                continue


def copy_ignored_file(source: int, target: int, name: str) -> None:
    """Copy through directory descriptors, never replacing a destination entry."""
    path = Path(name)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
    ):
        raise GitCommandError("Ignored file paths must stay inside the workspace")
    with ExitStack() as parents:
        for part in path.parts[:-1]:
            source = parents.enter_context(directory(Path(part), parent=source))
            with suppress(FileExistsError):
                os.mkdir(part, dir_fd=target)
            target = parents.enter_context(directory(Path(part), parent=target))
        try:
            os.stat(path.name, dir_fd=target, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return
        info = os.stat(path.name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(path.name, dir_fd=source), path.name, dir_fd=target)
            return
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source
        )
        with os.fdopen(descriptor, "rb") as origin:
            info = os.fstat(origin.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise GitCommandError(
                    "Ignored transfers require regular files without hardlinks"
                )
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target,
            )
            with os.fdopen(descriptor, "wb") as destination:
                shutil.copyfileobj(origin, destination)
                destination.flush()
                os.fchmod(destination.fileno(), stat.S_IMODE(info.st_mode) & 0o777)
                os.utime(destination.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))
