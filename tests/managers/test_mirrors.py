"""OCTO-46 — every project gets a mirror, and a mirror is always a git repository.

A remote upstream is cloned and freshened by fetching; a directory upstream is
`git init`'d and freshened by committing the folder's state, so a folder that was
never a repository still gets branches, diffs, and history. Sync hides behind a
freshness window that starts at zero, serializes per mirror, and degrades to the
stale mirror — with a warning, never silently — when the upstream is unreachable.

No database anywhere here: a mirror is filesystem state, keyed by project name.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import pytest

from octomate.config.mirrors import GitIdentity, MirrorsConfig
from octomate.managers.mirrors import (
    GitCommandError,
    MirrorManager,
    run_git,
)
from octomate.schemas.project import DirectoryUpstream, Project, RemoteUpstream
from tests.support.dependencies import Probe, probing, runs
from tests.support.managers import a_project

# For commits the *tests* make in their upstream repos — a person's, distinct from
# the manager's own identity, so authorship assertions cannot pass by accident.
IDENTITY = (
    "-c",
    "user.name=someone",
    "-c",
    "user.email=someone@example.com",
    "-c",
    "commit.gpgsign=false",
)


def a_mirror_manager(tmp_path: Path, window: float = 0.0) -> MirrorManager:
    return MirrorManager(
        config=MirrorsConfig(freshness_window=window),
        mirrors_dir=tmp_path / "mirrors",
    )


def a_folder(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content)
    return path


async def commit_all(repo: Path, message: str = "commit") -> None:
    await run_git("add", "-A", cwd=repo)
    await run_git(*IDENTITY, "commit", "-m", message, cwd=repo)


async def a_git_repo(path: Path, files: dict[str, str]) -> Path:
    """A real repository to clone from — a local path is a perfectly good git URL,
    so no test touches the network."""
    a_folder(path, files)
    await run_git("init", "-b", "main", str(path))
    await commit_all(path)
    return path


def a_remote_project(repo: Path, name: str = "") -> Project:
    return Project(root=repo, name=name, upstream=RemoteUpstream(url=str(repo)))


async def commit_count(mirror: Path) -> int:
    return int(await run_git("rev-list", "--count", "HEAD", cwd=mirror))


async def test_a_remote_upstream_is_cloned_onto_its_default_branch(
    tmp_path: Path,
) -> None:
    repo = await a_git_repo(tmp_path / "upstream", {"readme.md": "hello"})
    manager = a_mirror_manager(tmp_path)

    mirror = await manager.sync(a_remote_project(repo, name="inky"))

    assert mirror == tmp_path / "mirrors" / "inky"
    assert (await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=mirror)).strip() == (
        "main"
    )
    assert (mirror / "readme.md").read_text() == "hello"


async def test_a_remote_mirror_follows_new_upstream_commits(tmp_path: Path) -> None:
    repo = await a_git_repo(tmp_path / "upstream", {"readme.md": "hello"})
    manager = a_mirror_manager(tmp_path)
    project = a_remote_project(repo)
    await manager.sync(project)

    (repo / "next.md").write_text("more")
    await commit_all(repo)
    mirror = await manager.sync(project)

    assert (mirror / "next.md").read_text() == "more"
    assert (await run_git("rev-parse", "HEAD", cwd=mirror)) == (
        await run_git("rev-parse", "HEAD", cwd=repo)
    )


async def test_a_directory_upstream_first_commit_is_the_folder(tmp_path: Path) -> None:
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep", "deep/nested.md": "also"})
    manager = a_mirror_manager(tmp_path)

    mirror = await manager.sync(a_project(folder))

    assert (mirror / "notes.md").read_text() == "keep"
    assert (mirror / "deep" / "nested.md").read_text() == "also"
    assert await commit_count(mirror) == 1
    # The folder stays a folder: syncing it must not turn it into a repository.
    assert not (folder / ".git").exists()


async def test_a_directory_mirror_syncs_from_the_upstream_not_the_root(
    tmp_path: Path,
) -> None:
    folder = a_folder(tmp_path / "elsewhere", {"real.md": "content"})
    a_folder(tmp_path / "root", {"decoy.md": "not this"})
    project = Project(
        root=tmp_path / "root",
        name="docs",
        upstream=DirectoryUpstream(path=folder),
    )

    mirror = await a_mirror_manager(tmp_path).sync(project)

    assert (mirror / "real.md").exists()
    assert not (mirror / "decoy.md").exists()


async def test_syncing_an_unchanged_folder_creates_no_commit(tmp_path: Path) -> None:
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep"})
    manager = a_mirror_manager(tmp_path)
    project = a_project(folder)

    await manager.sync(project)
    mirror = await manager.sync(project)

    assert await commit_count(mirror) == 1


async def test_a_deletion_in_the_folder_arrives_as_a_deletion(tmp_path: Path) -> None:
    folder = a_folder(tmp_path / "docs", {"keep.md": "one", "drop.md": "two"})
    manager = a_mirror_manager(tmp_path)
    project = a_project(folder)
    await manager.sync(project)

    (folder / "drop.md").unlink()
    (folder / "keep.md").write_text("edited")
    mirror = await manager.sync(project)

    assert not (mirror / "drop.md").exists()
    assert (mirror / "keep.md").read_text() == "edited"
    assert await commit_count(mirror) == 2


async def test_the_folders_own_gitignore_is_honored(tmp_path: Path) -> None:
    # What keeps a served checkout's `.octomate/` — database, keys — out of its
    # own mirror: `add -A` reads ignore rules from the folder it stages.
    folder = a_folder(
        tmp_path / "docs",
        {".gitignore": "secrets/\n", "secrets/key.json": "{}", "readme.md": "public"},
    )

    mirror = await a_mirror_manager(tmp_path).sync(a_project(folder))

    assert (mirror / "readme.md").exists()
    assert not (mirror / "secrets").exists()


async def test_sync_commits_default_to_the_machine_identity(tmp_path: Path) -> None:
    # The machine's record of the folder, never attributed to whoever's dotfiles
    # are installed — and made with `-c`, so nothing persists into the mirror's
    # config for a later workspace to inherit.
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep"})

    mirror = await a_mirror_manager(tmp_path).sync(a_project(folder))

    author = await run_git("log", "-1", "--format=%an <%ae>", cwd=mirror)
    assert author.strip() == "octomate <octomate@example.com>"
    assert "user.name" not in (mirror / ".git" / "config").read_text()


async def test_sync_commits_carry_the_configured_identity(tmp_path: Path) -> None:
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep"})
    manager = MirrorManager(
        config=MirrorsConfig(
            identity=GitIdentity(name="Lu Hui", email="lu@example.com")
        ),
        mirrors_dir=tmp_path / "mirrors",
    )

    mirror = await manager.sync(a_project(folder))

    author = await run_git("log", "-1", "--format=%an <%ae>", cwd=mirror)
    assert author.strip() == "Lu Hui <lu@example.com>"


async def test_a_failed_fetch_degrades_to_the_stale_mirror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = await a_git_repo(tmp_path / "upstream", {"readme.md": "hello"})
    manager = a_mirror_manager(tmp_path)
    project = a_remote_project(repo, name="inky")
    mirror = await manager.sync(project)

    shutil.rmtree(repo)
    with caplog.at_level(logging.WARNING):
        assert await manager.sync(project) == mirror

    assert "mirror for inky is stale" in caplog.text
    assert (mirror / "readme.md").read_text() == "hello"


async def test_a_missing_folder_degrades_to_the_stale_mirror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep"})
    manager = a_mirror_manager(tmp_path)
    project = a_project(folder)
    mirror = await manager.sync(project)

    shutil.rmtree(folder)
    with caplog.at_level(logging.WARNING):
        assert await manager.sync(project) == mirror

    assert "is stale" in caplog.text
    assert (mirror / "notes.md").read_text() == "keep"


async def test_a_mirror_that_cannot_be_created_is_a_hard_failure(
    tmp_path: Path,
) -> None:
    # There is nothing to fall back to — and nothing may be left behind either,
    # since a partial mirror would read as existing and then always be "stale".
    project = a_remote_project(tmp_path / "missing", name="ghost")
    manager = a_mirror_manager(tmp_path)

    with pytest.raises(GitCommandError):
        await manager.sync(project)

    assert not (tmp_path / "mirrors" / "ghost").exists()


async def test_a_fresh_mirror_is_not_resynced_within_the_window(
    tmp_path: Path,
) -> None:
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep"})
    manager = a_mirror_manager(tmp_path, window=3600.0)
    project = a_project(folder)
    await manager.sync(project)

    (folder / "late.md").write_text("after the sync")
    mirror = await manager.sync(project)

    assert not (mirror / "late.md").exists()
    assert await commit_count(mirror) == 1


async def test_a_synced_mirror_installs_its_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Which is what makes forking one cheap: the machine's package store is warm
    # and the installed trees themselves are what a fork copies.
    probing(monkeypatch)
    folder = a_folder(tmp_path / "docs", {Probe.lockfile: "one"})
    manager = a_mirror_manager(tmp_path)

    mirror = await manager.sync(a_project(folder))

    assert runs(mirror) == 1


async def test_a_mirror_whose_lockfile_stood_still_is_not_installed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The freshness window starts at zero, so every fork syncs; installing on
    # every one of those would rebuild an environment nothing has changed.
    probing(monkeypatch)
    folder = a_folder(tmp_path / "docs", {Probe.lockfile: "one"})
    manager = a_mirror_manager(tmp_path)
    project = a_project(folder)
    await manager.sync(project)

    (folder / "notes.md").write_text("unrelated")
    mirror = await manager.sync(project)

    assert (mirror / "notes.md").read_text() == "unrelated"
    assert runs(mirror) == 1


async def test_a_moved_lockfile_installs_the_mirror_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probing(monkeypatch)
    folder = a_folder(tmp_path / "docs", {Probe.lockfile: "one"})
    manager = a_mirror_manager(tmp_path)
    project = a_project(folder)
    await manager.sync(project)

    (folder / Probe.lockfile).write_text("two")
    mirror = await manager.sync(project)

    assert runs(mirror) == 2


async def test_two_syncs_of_one_mirror_serialize(tmp_path: Path) -> None:
    # Ten threads opened at once must not mean ten fetches of one mirror — and
    # never two git processes racing on one index.
    folder = a_folder(tmp_path / "docs", {"notes.md": "keep"})
    manager = a_mirror_manager(tmp_path)
    project = a_project(folder)
    await manager.sync(project)

    lock = manager.locks[project.name]
    await lock.acquire()
    second = asyncio.create_task(manager.sync(project))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()

    lock.release()
    assert await second == manager.path(project)


async def test_reconcile_mirrors_every_enabled_project(tmp_path: Path) -> None:
    folder = a_folder(tmp_path / "inky", {"readme.md": "hello"})
    manager = MirrorManager(mirrors_dir=tmp_path / "mirrors")

    await manager.reconcile(
        [a_project(folder), a_project(tmp_path / "gone", enabled=False)]
    )

    assert (tmp_path / "mirrors" / "inky" / ".git").is_dir()
    assert not (tmp_path / "mirrors" / "gone").exists()


async def test_reconcile_continues_past_a_failing_project(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = a_remote_project(tmp_path / "missing", name="bad")
    good = a_project(a_folder(tmp_path / "good", {"readme.md": "hello"}))
    manager = MirrorManager(mirrors_dir=tmp_path / "mirrors")

    with caplog.at_level(logging.ERROR):
        await manager.reconcile([bad, good])

    assert (tmp_path / "mirrors" / "good" / ".git").is_dir()
    assert "mirror for bad could not be made" in caplog.text


async def test_the_blank_mirror_is_an_empty_repository_with_a_head(
    tmp_path: Path,
) -> None:
    # OCTO-50: what a thread in no project forks its workspace from — a mirror with
    # no upstream, made by the same `create` as every other. A repository with no
    # commit forks onto an unborn branch, where git refuses the ordinary things a
    # run does with one, so it gets an empty commit and nothing else.
    manager = a_mirror_manager(tmp_path)

    blank = await manager.create()

    assert blank == (tmp_path / "mirrors" / ".blank").resolve() == manager.path()
    assert await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=blank) == "main\n"
    assert await run_git("ls-files", cwd=blank) == ""
    # The machine's own identity, so a fresh server commits without a
    # `git config --global` first.
    assert await run_git("log", "--format=%an", cwd=blank) == "octomate\n"


async def test_the_blank_mirror_is_made_once(tmp_path: Path) -> None:
    # It has no upstream, so there is nothing for it to be behind and nothing a
    # second call should do to it — including to the commit a fork resolves HEAD to.
    manager = a_mirror_manager(tmp_path)
    blank = await manager.create()
    head = await run_git("rev-parse", "HEAD", cwd=blank)

    assert await manager.create() == blank
    assert await run_git("rev-parse", "HEAD", cwd=blank) == head


async def test_the_blank_mirror_is_not_a_projects(tmp_path: Path) -> None:
    # Nothing binds to it and it takes no registry row, so a project may be called
    # anything without landing on top of it.
    manager = a_mirror_manager(tmp_path)
    folder = a_folder(tmp_path / "blank", {"readme.md": "hello"})

    await manager.create()
    mirror = await manager.sync(a_project(folder))

    assert mirror != await manager.create()
    assert await run_git("ls-files", cwd=mirror) == "readme.md\n"
