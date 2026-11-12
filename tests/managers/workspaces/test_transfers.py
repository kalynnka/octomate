from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from octomate.managers.workspaces.mirrors import GitCommandError, run_git
from octomate.managers.workspaces.transfers import copy_ignored, copy_ignored_files


async def test_ignored_transfer_preserves_files_and_skips_directories(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    target.mkdir()
    await run_git("init", "--template=", str(source), isolated=True)
    (source / ".gitignore").write_text("*.local\n.env\nbuild/\n")
    (source / ".git/info").mkdir()
    (source / ".git/info/exclude").write_text("settings\n")
    (source / "nested").mkdir()
    (source / "nested/secret.local").write_bytes(b"secret\x00bytes")
    (source / "nested/secret.local").chmod(0o600)
    (source / ".env").write_text("configuration")
    (source / "settings").write_text("local exclude")
    (source / "tracked.local").write_text("staged ignored file")
    await run_git("add", "-f", "tracked.local", cwd=source)
    await run_git("update-index", "--split-index", cwd=source)
    index = (source / ".git/index").read_bytes()
    (source / "untracked").write_text("not ignored")
    (source / "build").mkdir()
    (source / "build/output").write_text("regenerable")
    outside = tmp_path / "outside"
    outside.write_text("host data")
    (source / "link.local").symlink_to(outside)

    await copy_ignored(source, target)

    assert (target / "nested/secret.local").read_bytes() == b"secret\x00bytes"
    assert stat.S_IMODE((target / "nested/secret.local").stat().st_mode) == 0o600
    assert (target / ".env").read_text() == "configuration"
    assert (target / "settings").read_text() == "local exclude"
    assert (target / "link.local").readlink() == outside
    assert not (target / "build").exists()
    assert not (target / "tracked.local").exists()
    assert not (target / "untracked").exists()
    assert (source / ".git/index").read_bytes() == index


async def test_ignored_transfer_does_not_run_fsmonitor_or_use_external_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    target.mkdir()
    await run_git("init", "--template=", str(source), isolated=True)
    (source / ".gitignore").write_text(".env\n")
    (source / ".env").write_text("configuration")
    (source / "host-ignored").write_text("not a workspace exclusion")
    await run_git("add", ".gitignore", cwd=source)
    marker = tmp_path / "executed"
    hook = tmp_path / "fsmonitor"
    hook.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\n')
    hook.chmod(0o755)
    await run_git("config", "core.fsmonitor", str(hook), cwd=source)
    # Git has a default global ignore file even when global config is disabled.
    xdg = tmp_path / "config"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git/ignore").write_text("host-ignored\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "redirected"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "outside"))

    await copy_ignored(source, target)

    assert not marker.exists()
    assert (target / ".env").read_text() == "configuration"
    assert not (target / "host-ignored").exists()


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("nested", [False, True])
def test_ignored_transfer_refuses_symlinked_roots_and_parents(
    tmp_path: Path, side: str, nested: bool
) -> None:
    source, target, outside = (
        tmp_path / name for name in ("source", "target", "outside")
    )
    source.mkdir()
    target.mkdir()
    outside.mkdir()
    path = "nested/.env" if nested else ".env"
    (source / path).parent.mkdir(exist_ok=True)
    (source / path).write_text("private configuration")
    redirected = tmp_path / side
    if nested:
        redirected /= "nested"
    if redirected.exists():
        redirected.rename(tmp_path / "original")
    redirected.symlink_to(outside, target_is_directory=True)
    (outside / ".env").write_text("host-only configuration")

    with pytest.raises(
        OSError, match=r"Not a directory|Too many levels of symbolic links"
    ):
        copy_ignored_files(source, target, [path])

    assert (outside / ".env").read_text() == "host-only configuration"
    if side == "source":
        assert not (target / path).exists()


@pytest.mark.parametrize("entry", ["file", "symlink", "dangling", "directory"])
def test_ignored_transfer_keeps_existing_destination_entries(
    tmp_path: Path, entry: str
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / ".env").write_text("source")
    destination = target / ".env"
    if entry in {"symlink", "dangling"}:
        outside = tmp_path / "outside"
        if entry == "symlink":
            outside.write_text("destination")
        destination.symlink_to(outside)
    elif entry == "directory":
        destination.mkdir()
    else:
        destination.write_text("destination")
    before = destination.lstat()

    copy_ignored_files(source, target, [".env"])

    assert destination.lstat() == before
    if entry in {"file", "symlink"}:
        assert destination.read_text() == "destination"


@pytest.mark.parametrize("entry", ["hardlink", "fifo", "directory"])
def test_ignored_transfer_refuses_special_files_and_hardlinks(
    tmp_path: Path, entry: str
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    path = source / ".env"
    if entry == "hardlink":
        outside = tmp_path / "outside"
        outside.write_text("host data")
        path.hardlink_to(outside)
    elif entry == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    with pytest.raises((GitCommandError, OSError)):
        copy_ignored_files(source, target, [".env"])

    assert not (target / ".env").exists()


def test_ignored_transfer_does_not_follow_a_destination_created_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / ".env").write_text("private configuration")
    outside = tmp_path / "outside"
    original_open = os.open

    def replacing_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == ".env" and flags & os.O_CREAT:
            os.symlink(outside, path, dir_fd=dir_fd)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)

    copy_ignored_files(source, target, [".env"])

    assert not outside.exists()
    assert (target / ".env").readlink() == outside


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", ".git/config", "nested/.GIT/HEAD", "."]
)
def test_ignored_transfer_rejects_paths_outside_working_files(
    tmp_path: Path, name: str
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()

    with pytest.raises(GitCommandError, match="inside the workspace"):
        copy_ignored_files(source, target, [name])

    assert list(target.iterdir()) == []
