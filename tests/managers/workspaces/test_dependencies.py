"""A tree installs from its lockfile, and only when that lockfile moves.

Most tests use `tests.support.dependencies`'s probe to check manager selection,
installation stamps and failed commands. The uv tests use real, offline installs
to check that refreshing dependencies preserves an existing environment.

The `.git` directory in every tree is not decoration: the stamp lives there, which
is what makes a copied fork skip the install and a cloned one run it.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from octomate.managers.workspaces.dependencies import (
    STAMP,
    UV,
    Npm,
    PackageManager,
    Pnpm,
    install,
    manager,
)
from tests.support.dependencies import FAILS, RAN, Probe, probing, runs


@pytest.fixture(autouse=True)
def probe(monkeypatch: pytest.MonkeyPatch) -> None:
    probing(monkeypatch)


def a_tree(path: Path, lock: str = Probe.lockfile, content: str = "one") -> Path:
    """A tree as a fork or a mirror arrives: a lockfile, and a `.git` to stamp."""
    (path / ".git").mkdir(parents=True, exist_ok=True)
    (path / lock).write_text(content)
    return path


@pytest.fixture
async def uv_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("UV_") or name in {
            "VIRTUAL_ENV",
            "PYTHONHOME",
            "PYTHONPATH",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "workspace-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
    )
    assert await UV().run(tmp_path, "uv", "lock")
    return tmp_path


@pytest.mark.parametrize("owner", [UV(), Pnpm(), Npm()])
def test_the_lockfile_says_whose_tree_it_is(
    tmp_path: Path, owner: PackageManager
) -> None:
    tree = a_tree(tmp_path, lock=owner.lockfile)

    assert type(manager(tree)) is type(owner)


def test_a_tree_with_no_lockfile_belongs_to_nobody(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    assert manager(tmp_path) is None


def test_a_tree_carrying_two_lockfiles_is_answered_by_the_first(
    tmp_path: Path,
) -> None:
    """A repository with both a Python and a Node lockfile installs the one listed
    first rather than both. Recorded because it is a choice, not a discovery."""
    tree = a_tree(tmp_path, lock=UV.lockfile)
    (tree / Npm.lockfile).write_text("{}")

    assert isinstance(manager(tree), UV)


async def test_an_install_runs_in_the_tree(tmp_path: Path) -> None:
    tree = a_tree(tmp_path)

    await install(tree)

    assert runs(tree) == 1
    assert (tree / ".git" / STAMP).is_file()


async def test_an_installed_tree_is_not_installed_again(tmp_path: Path) -> None:
    """What the mirror relies on: the freshness window is zero by default, so
    every fork syncs, and a sync that installed every time would rebuild the
    environment for a lockfile nothing has touched."""
    tree = a_tree(tmp_path)

    await install(tree)
    await install(tree)

    assert runs(tree) == 1


async def test_a_moved_lockfile_installs_again(tmp_path: Path) -> None:
    tree = a_tree(tmp_path)
    await install(tree)

    (tree / Probe.lockfile).write_text("two")
    await install(tree)

    assert runs(tree) == 2


async def test_a_failing_install_is_logged_and_leaves_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    probing(monkeypatch, FAILS)
    tree = a_tree(tmp_path)

    with caplog.at_level(logging.WARNING):
        await install(tree)

    assert "nope" in caplog.text
    # No stamp, so the next tree forked from this one tries again rather than
    # inheriting the claim that it is installed.
    assert not (tree / ".git" / STAMP).exists()


async def test_a_host_without_the_tool_loses_its_dependencies_not_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    probing(monkeypatch, ("octomate-installs-nothing",))
    tree = a_tree(tmp_path)

    with caplog.at_level(logging.WARNING):
        await install(tree)

    assert "octomate-installs-nothing" in caplog.text
    assert not (tree / ".git" / STAMP).exists()


async def test_a_later_command_does_not_run_after_an_earlier_one_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`uv sync` into a venv `uv venv` could not create is a second failure
    reporting the first, and the log should carry the one that happened."""
    probing(monkeypatch, FAILS, RAN)
    tree = a_tree(tmp_path)

    with caplog.at_level(logging.WARNING):
        await install(tree)

    assert runs(tree) == 0


async def test_uv_creates_a_relocatable_environment(uv_tree: Path) -> None:
    await install(uv_tree)

    assert "relocatable = true" in (uv_tree / ".venv/pyvenv.cfg").read_text()
    assert (uv_tree / ".git" / STAMP).is_file()


async def test_uv_refresh_preserves_the_environment_after_a_lockfile_change(
    uv_tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await install(uv_tree)
    stamp = uv_tree / ".git" / STAMP
    previous_stamp = stamp.read_text()
    marker = uv_tree / ".venv/preserved"
    marker.write_text("keep this environment")
    config = uv_tree / ".venv/pyvenv.cfg"
    previous_config = config.stat().st_mtime_ns
    project = uv_tree / "pyproject.toml"
    project.write_text(project.read_text().replace('"0.1.0"', '"0.2.0"'))
    assert await UV().run(uv_tree, "uv", "lock")

    with caplog.at_level(logging.WARNING):
        await install(uv_tree)

    assert marker.read_text() == "keep this environment"
    assert config.stat().st_mtime_ns == previous_config
    assert stamp.read_text() != previous_stamp
    assert not caplog.records
    previous_time = stamp.stat().st_mtime_ns
    await install(uv_tree)
    assert stamp.stat().st_mtime_ns == previous_time


async def test_uv_failed_sync_preserves_the_previous_stamp(
    uv_tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await install(uv_tree)
    stamp = uv_tree / ".git" / STAMP
    previous_stamp = stamp.read_text()
    lockfile = uv_tree / UV.lockfile
    lockfile.write_text(lockfile.read_text() + "\n# Refresh dependencies.\n")
    project = uv_tree / "pyproject.toml"
    project.write_text(
        project.read_text()
        + '\ndependencies = ["missing-package"]\n'
        + '\n[tool.uv.sources]\nmissing-package = { path = "absent" }\n'
    )

    with caplog.at_level(logging.WARNING):
        await install(uv_tree)

    assert "`uv sync`" in caplog.text
    assert stamp.read_text() == previous_stamp


async def test_uv_preserves_and_rejects_a_non_relocatable_environment(
    uv_tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert await UV().run(uv_tree, "uv", "venv")
    marker = uv_tree / ".venv/preserved"
    marker.write_text("keep this environment")
    config = uv_tree / ".venv/pyvenv.cfg"
    previous_config = config.read_text()

    with caplog.at_level(logging.WARNING):
        await install(uv_tree)

    assert marker.read_text() == "keep this environment"
    assert config.read_text() == previous_config
    assert "not relocatable" in caplog.text
    assert not (uv_tree / ".git" / STAMP).exists()
