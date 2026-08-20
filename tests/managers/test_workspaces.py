"""OCTO-47 — a thread's workspace is a fork of its project's mirror.

Every test here runs twice, once per mechanism: the copy-on-write fork where the
filesystem under the test host offers one, and the `git clone` fallback always,
since a host that can clone extents must still be able to fall back. What both
have to produce is the same thing — a complete independent repository on the
thread's branch, whose commits reach nothing outside it.

No database anywhere here: a workspace is filesystem state, keyed by thread id.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import pytest
from uuid_utils.compat import uuid7

from octomate.managers import workspaces
from octomate.managers.mirrors import GitCommandError, run_git
from octomate.managers.workspaces import CopyError, WorkspaceManager

# For commits the tests make in their mirrors — nothing in this unit commits, so
# any authorship a workspace shows came from the tree it was forked from.
IDENTITY = (
    "-c",
    "user.name=someone",
    "-c",
    "user.email=someone@example.com",
    "-c",
    "commit.gpgsign=false",
)


@pytest.fixture(params=["copy-on-write", "clone"])
async def manager(request: pytest.FixtureRequest, tmp_path: Path) -> WorkspaceManager:
    """A manager per mechanism this host can actually fork with. The clone case is
    forced rather than probed, so a copy-on-write workstation still exercises what
    a plain ext4 server does; the copy case is skipped where the filesystem the
    workspaces land on has no clone to offer."""
    made = WorkspaceManager(workspaces_dir=tmp_path / "workspaces")
    if request.param == "clone":
        made.reflink, made.probed = None, True
    elif await made.detect() is None:
        pytest.skip("no copy-on-write filesystem under the test host's tmp_path")
    return made


async def a_mirror(path: Path, files: dict[str, str]) -> Path:
    """A pristine mirror to fork from — what `MirrorManager.sync` leaves behind,
    built here directly so this unit's tests turn on nothing but git."""
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (path / name).write_text(content)
    await run_git("init", "-b", "main", str(path))
    await run_git("add", "-A", cwd=path)
    await run_git(*IDENTITY, "commit", "-m", "mirror", cwd=path)
    return path


async def head(repo: Path) -> str:
    return (await run_git("rev-parse", "HEAD", cwd=repo)).strip()


async def branch(repo: Path) -> str:
    return (await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)).strip()


async def common_dir(repo: Path) -> Path:
    """Where this repository's objects and refs actually live. A `git worktree`
    answers with the parent repository's `.git`; an independent repository — which
    is the whole requirement — answers with its own."""
    return Path(
        (
            await run_git(
                "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo
            )
        ).strip()
    )


async def test_a_workspace_is_an_independent_repository_on_the_threads_branch(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    thread = uuid7()

    workspace = await manager.materialize(thread, mirror)

    assert workspace == (tmp_path / "workspaces" / str(thread)).resolve()
    assert (workspace / "readme.md").read_text() == "hello"
    assert await branch(workspace) == f"octomate/thread-{thread}"
    assert await common_dir(workspace) == workspace / ".git"


async def test_a_commit_in_a_workspace_writes_only_inside_it(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    before = sorted(path.relative_to(mirror) for path in (mirror / ".git").rglob("*"))
    workspace = await manager.materialize(uuid7(), mirror)

    (workspace / "work.md").write_text("done")
    await run_git("add", "-A", cwd=workspace)
    await run_git(*IDENTITY, "commit", "-m", "work", cwd=workspace)

    assert await head(workspace) != await head(mirror)
    assert (
        sorted(path.relative_to(mirror) for path in (mirror / ".git").rglob("*"))
        == before
    )
    assert not (mirror / "work.md").exists()


async def test_two_threads_on_one_project_fork_into_separate_directories(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    one, other = uuid7(), uuid7()

    first = await manager.materialize(one, mirror)
    second = await manager.materialize(other, mirror)

    assert first != second
    (first / "mine.md").write_text("only here")
    assert not (second / "mine.md").exists()
    assert await branch(second) == f"octomate/thread-{other}"


async def test_materializing_again_answers_the_live_tree(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The turn after the first one asks for the same workspace, and the work in it
    # is uncommitted more often than not.
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    thread = uuid7()
    workspace = await manager.materialize(thread, mirror)
    (workspace / "wip.md").write_text("half done")

    assert await manager.materialize(thread, mirror) == workspace
    assert (workspace / "wip.md").read_text() == "half done"


async def test_releasing_removes_the_workspace_and_leaves_the_mirror(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    thread = uuid7()
    workspace = await manager.materialize(thread, mirror)
    (workspace / "work.md").write_text("done")

    await manager.release(thread)

    assert not workspace.exists()
    assert (mirror / "readme.md").read_text() == "hello"
    assert await run_git("status", "--porcelain", cwd=mirror) == ""


async def test_releasing_a_workspace_that_is_not_there_is_no_work(
    manager: WorkspaceManager,
) -> None:
    # Pruning a thread that never had a workspace, and releasing one twice, both
    # arrive here.
    await manager.release(uuid7())


async def test_a_fork_that_fails_leaves_nothing_behind(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    thread = uuid7()

    with pytest.raises((CopyError, GitCommandError)):
        await manager.materialize(thread, tmp_path / "no-such-mirror")

    assert not manager.path(thread).exists()
    assert list((tmp_path / "workspaces").iterdir()) == []


async def test_what_a_killed_fork_left_is_never_a_workspace(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The case the cleanup cannot reach: a machine that dies mid-copy leaves the
    # half-copied tree behind. It is not where the workspace goes, and the next
    # fork of this thread reclaims it rather than building on it.
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    thread = uuid7()
    abandoned = (tmp_path / "workspaces" / f".{thread}.forking").resolve()
    abandoned.mkdir(parents=True)
    (abandoned / "half.md").write_text("copied before the power went")

    workspace = await manager.materialize(thread, mirror)

    assert (workspace / "readme.md").read_text() == "hello"
    assert not (workspace / "half.md").exists()
    assert not abandoned.exists()


async def test_two_materializations_of_one_thread_serialize(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # Two turns arriving together must not have two `cp`s racing into one path.
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    thread = uuid7()
    await manager.materialize(thread, mirror)

    lock = manager.locks[thread]
    await lock.acquire()
    second = asyncio.create_task(manager.materialize(thread, mirror))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()

    lock.release()
    assert await second == manager.path(thread)


async def test_the_mechanism_is_probed_once_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = WorkspaceManager(workspaces_dir=tmp_path / "workspaces")

    with caplog.at_level(logging.INFO):
        first = await manager.detect()
        assert await manager.detect() == first

    assert len([line for line in caplog.messages if "workspaces fork by" in line]) == 1


async def test_a_host_without_copy_on_write_forks_by_cloning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The ext4 server, wherever the tests run: every clone flag refused, and a
    # workspace all the same.
    monkeypatch.setattr(workspaces, "REFLINK_FLAGS", ("--not-a-flag",))
    manager = WorkspaceManager(workspaces_dir=tmp_path / "workspaces")
    mirror = await a_mirror(tmp_path / "mirror", {"readme.md": "hello"})
    thread = uuid7()

    with caplog.at_level(logging.INFO):
        workspace = await manager.materialize(thread, mirror)

    assert manager.reflink is None
    assert "thread workspaces fork by git clone" in caplog.text
    assert (workspace / "readme.md").read_text() == "hello"
    assert await common_dir(workspace) == workspace / ".git"


async def test_a_workspace_path_is_absolute_from_a_relative_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default `.octomate/workspaces` is relative to wherever Octomate was
    # started, and a workspace is handed to processes started elsewhere.
    monkeypatch.chdir(tmp_path)
    manager = WorkspaceManager()
    thread = uuid.uuid4()

    assert manager.path(thread) == (tmp_path / ".octomate/workspaces" / str(thread))
