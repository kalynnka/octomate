"""OCTO-47, OCTO-51, OCTO-52 — a thread's workspace is a fork of its project's
mirror, and a disposable one.

Every test here runs twice, once per mechanism: the copy-on-write fork where the
filesystem under the test host offers one, and the `git clone` fallback always,
since a host that can clone extents must still be able to fall back. What both
have to produce is the same thing — a complete independent repository on the
thread's branch, whose commits reach nothing outside it.

What makes it disposable is that every turn is snapshotted and pushed to the
mirror as the thread's own ref, so a workspace that goes away costs a resume
rather than the work — which is also the half a copied fork and a cloned one
disagree about, since a copy carries the mirror's refs and a clone fetches none.

A snapshot is not a commit on the agent's branch, and the tests below are mostly
about the difference: the repository comes back as the agent left it, history and
uncommitted mess alike, with nothing added to it on its behalf.

The rows are real, because `save` resolves where to push through the thread's
project: a bare uuid has no mirror behind it. Everything below that seam still
works in paths — the fork, the snapshot, the sweep — and is exercised directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.mirrors import GitIdentity, MirrorsConfig
from octomate.config.workspaces import WorkspacesConfig
from octomate.managers import workspaces
from octomate.managers.mirrors import GitCommandError, MirrorManager, run_git
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import (
    ChatWorkspace,
    CopyError,
    ProjectWorkspace,
    WorkspaceManager,
)
from octomate.schemas.thread import Thread, ThreadKey
from tests.support.managers import a_project, a_registry

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


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def a_root(tmp_path: Path) -> Path:
    """The project's root on disk. It must exist, or `reconcile` disables the
    project and a thread filed under it resolves to none."""
    root = tmp_path / "inky"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def a_manager(
    tmp_path: Path, identity: GitIdentity | None = None
) -> WorkspaceManager:
    """A manager over its own temporary directories, with one project registered.

    Real collaborators rather than fakes: the registry is what turns a thread into
    a project, the mirrors are where that project's lives, and the identity a
    snapshot is written under is the mirrors' rather than this manager's to hold.
    """
    return WorkspaceManager(
        projects=await a_registry(a_project(a_root(tmp_path))),
        mirrors=MirrorManager(
            config=MirrorsConfig(identity=identity) if identity else MirrorsConfig(),
            mirrors_dir=tmp_path / "mirrors",
        ),
        workspaces_dir=tmp_path / "workspaces",
    )


async def a_bound_thread(manager: WorkspaceManager) -> Thread:
    """A thread of the one kind that carries a project, filed under the registered
    one. `save` finds the mirror to push to through this, so a bare uuid has
    nowhere to go."""
    return await ThreadManager(users=UserManager()).ensure(
        ThreadKey("test", "thread", "chat", str(uuid7())),
        project=manager.projects.get("inky"),
    )


def a_workspace(manager: WorkspaceManager, thread_id: uuid.UUID) -> ProjectWorkspace:
    """The handle a run is given before anything is forked. `materialize` takes one
    rather than a thread id, because where a workspace goes and what it is forked
    from are the workspace's to answer, not the caller's."""
    project = manager.projects.get("inky")
    assert project is not None
    return ProjectWorkspace(manager, thread_id, project)


async def a_project_mirror(manager: WorkspaceManager, files: dict[str, str]) -> Path:
    """The mirror `MirrorManager.sync` would leave for the registered project,
    built here directly so this unit turns on nothing but git."""
    project = manager.projects.get("inky")
    assert project is not None
    return await a_mirror(manager.mirrors.path(project), files)


@pytest.fixture(params=["copy-on-write", "clone"])
async def manager(request: pytest.FixtureRequest, tmp_path: Path) -> WorkspaceManager:
    """A manager per mechanism this host can actually fork with. The clone case is
    forced rather than probed, so a copy-on-write workstation still exercises what
    a plain ext4 server does; the copy case is skipped where the filesystem the
    workspaces land on has no clone to offer."""
    made = await a_manager(tmp_path)
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


async def commit_count(repo: Path) -> int:
    return int(await run_git("rev-list", "--count", "HEAD", cwd=repo))


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
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)

    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert workspace == (tmp_path / "workspaces" / str(thread.id)).resolve()
    assert (workspace / "readme.md").read_text() == "hello"
    assert await branch(workspace) == f"octomate/thread-{thread.id}"
    assert await common_dir(workspace) == workspace / ".git"


async def test_a_commit_in_a_workspace_writes_only_inside_it(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    before = sorted(path.relative_to(mirror) for path in (mirror / ".git").rglob("*"))
    workspace = await manager.materialize(a_workspace(manager, uuid7()), mirror)

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
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    one = await a_bound_thread(manager)
    other = await a_bound_thread(manager)

    first = await manager.materialize(a_workspace(manager, one.id), mirror)
    second = await manager.materialize(a_workspace(manager, other.id), mirror)

    assert first != second
    (first / "mine.md").write_text("only here")
    assert not (second / "mine.md").exists()
    assert await branch(second) == f"octomate/thread-{other.id}"


async def test_materializing_again_answers_the_live_tree(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The turn after the first one asks for the same workspace, and the work in it
    # is uncommitted more often than not.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    (workspace / "wip.md").write_text("half done")

    assert (
        await manager.materialize(a_workspace(manager, thread.id), mirror) == workspace
    )
    assert (workspace / "wip.md").read_text() == "half done"


async def test_releasing_removes_the_workspace_and_leaves_the_mirror(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    (workspace / "work.md").write_text("done")

    await manager.release(thread.id)

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
    thread = await a_bound_thread(manager)

    with pytest.raises((CopyError, GitCommandError)):
        await manager.materialize(
            a_workspace(manager, thread.id), tmp_path / "no-such-mirror"
        )

    assert not manager.path(thread.id).exists()
    assert list((tmp_path / "workspaces").iterdir()) == []


async def test_what_a_killed_fork_left_is_never_a_workspace(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The case the cleanup cannot reach: a machine that dies mid-copy leaves the
    # half-copied tree behind. It is not where the workspace goes, and the next
    # fork of this thread reclaims it rather than building on it.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    abandoned = (tmp_path / "workspaces" / f".{thread.id}.forking").resolve()
    abandoned.mkdir(parents=True)
    (abandoned / "half.md").write_text("copied before the power went")

    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert (workspace / "readme.md").read_text() == "hello"
    assert not (workspace / "half.md").exists()
    assert not abandoned.exists()


async def test_two_materializations_of_one_thread_serialize(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # Two turns arriving together must not have two `cp`s racing into one path.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    await manager.materialize(a_workspace(manager, thread.id), mirror)

    lock = manager.locks[thread.id]
    await lock.acquire()
    second = asyncio.create_task(
        manager.materialize(a_workspace(manager, thread.id), mirror)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()

    lock.release()
    assert await second == manager.path(thread.id)


async def test_the_mechanism_is_probed_once_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = await a_manager(tmp_path)

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
    manager = await a_manager(tmp_path)
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)

    with caplog.at_level(logging.INFO):
        workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)

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


async def test_the_sweep_works_from_the_default_relative_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every other test here names an absolute directory, which is the one shape
    # production never uses: `.octomate/workspaces` hangs off wherever Octomate
    # was started. A relative root reaches git as a relative `GIT_INDEX_FILE`
    # against a workspace cwd, and resolves inside the workspace it describes.
    monkeypatch.chdir(tmp_path)
    manager = WorkspaceManager(
        projects=await a_registry(a_project(a_root(tmp_path))),
        mirrors=MirrorManager(mirrors_dir=Path(".octomate/mirrors")),
    )
    thread = await a_bound_thread(manager)
    project = manager.projects.get("inky")
    assert project is not None
    mirror = await a_mirror(manager.mirrors.path(project), {"readme.md": "hello"})
    await a_turn(
        await manager.materialize(a_workspace(manager, thread.id), mirror),
        {"work.md": "done"},
    )
    await manager.save(thread)

    assert await manager.prune(idle=0.0) == [thread.id]


async def a_turn(workspace: Path, files: dict[str, str]) -> None:
    """What a turn leaves on disk — the workspace's own writes, uncommitted, as an
    agent leaves them when its run ends."""
    for name, content in files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(content)


async def saved(mirror: Path, thread_id: uuid.UUID) -> str:
    """What the mirror is keeping for this thread, or "" when it keeps nothing."""
    return (
        await run_git("ls-remote", str(mirror), workspaces.thread_ref(thread_id))
    ).strip()


async def test_a_turn_leaves_its_work_on_the_threads_ref(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(workspace, {"work.md": "done"})

    await manager.save(thread)

    ref = workspaces.thread_ref(thread.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "done"
    # And the mirror's own branch is not where it landed.
    assert not (mirror / "work.md").exists()
    assert await branch(mirror) == "main"


async def their_own_branch(workspace: Path) -> None:
    """What an agent doing real work leaves behind: a branch it named itself, a
    commit of its own on it, and something half-finished on top."""
    await run_git("checkout", "-b", "feat/theirs", cwd=workspace)
    await a_turn(workspace, {"done.md": "committed"})
    await run_git("add", "-A", cwd=workspace)
    await run_git(*IDENTITY, "commit", "-m", "feat: real work", cwd=workspace)
    await a_turn(workspace, {"wip.md": "not yet"})


async def test_saving_a_turn_leaves_the_agents_repository_alone(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The whole reason a turn is snapshotted rather than committed: what the agent
    # did with git is the agent's, and saving its work is not an edit to it. A
    # commit here would land on the agent's branch, and the next turn's commit
    # would bury it in a history a person is going to read.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await their_own_branch(workspace)
    before = await head(workspace)

    await manager.save(thread)

    assert await head(workspace) == before
    assert await branch(workspace) == "feat/theirs"
    subjects = await run_git("log", "--format=%s", cwd=workspace)
    assert subjects.splitlines() == ["feat: real work", "mirror"]
    assert (await run_git("status", "--porcelain", cwd=workspace)).split() == [
        "??",
        "wip.md",
    ]


async def test_a_workspace_comes_back_on_the_branch_the_agent_made(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # And with the agent's own commit under it, rather than on a thread branch it
    # never chose. Resolving that history is the agent's job; the resume's job is
    # to hand back the repository it was working in.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await their_own_branch(workspace)
    await manager.save(thread)
    left = await head(workspace)

    await manager.release(thread.id)
    resumed = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert await branch(resumed) == "feat/theirs"
    assert await head(resumed) == left
    subjects = await run_git("log", "--format=%s", cwd=resumed)
    assert subjects.splitlines() == ["feat: real work", "mirror"]
    assert (resumed / "wip.md").read_text() == "not yet"


async def test_a_detached_head_comes_back_detached(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # An agent reproducing something against a tag or an older commit is on no
    # branch at all, which is a state to restore rather than one to correct.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await run_git("checkout", "--detach", "HEAD", cwd=workspace)
    await a_turn(workspace, {"probe.md": "why does it fail here"})
    await manager.save(thread)
    left = await head(workspace)

    await manager.release(thread.id)
    resumed = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert await branch(resumed) == "HEAD"
    assert await head(resumed) == left
    assert (resumed / "probe.md").read_text() == "why does it fail here"


async def test_a_turn_that_changed_nothing_still_names_the_ref(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The push is what a later prune relies on, so it happens whether or not the
    # turn had anything to commit.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)

    await manager.save(thread)

    assert await saved(mirror, thread.id)
    assert await commit_count(workspace) == 1


async def test_a_released_workspace_comes_back_with_its_work(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # Pruning is a disk decision: the thread resumes with the tree its last turn
    # left, on the same branch, from a fork that was made all over again — and
    # work the agent had not committed comes back uncommitted, since restoring it
    # any other way would be handing the agent a state it never made.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(workspace, {"work.md": "done", "deep/nested.md": "also"})
    await manager.save(thread)
    left = await head(workspace)

    await manager.release(thread.id)
    resumed = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert (resumed / "work.md").read_text() == "done"
    assert (resumed / "deep" / "nested.md").read_text() == "also"
    assert await head(resumed) == left
    assert await branch(resumed) == f"octomate/thread-{thread.id}"
    assert sorted(
        (await run_git("status", "--porcelain", cwd=resumed)).split()
    ) == sorted(["??", "deep/", "??", "work.md"])


async def test_a_modified_file_comes_back_modified(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # And a deleted one comes back deleted. The snapshot is the tree as it stood,
    # so a resume restores the diff the agent was looking at rather than a fork
    # that merely contains its files.
    mirror = await a_project_mirror(
        manager, {"readme.md": "hello", "gone.md": "not for long"}
    )
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    (workspace / "readme.md").write_text("changed")
    (workspace / "gone.md").unlink()
    await manager.save(thread)

    await manager.release(thread.id)
    resumed = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert (resumed / "readme.md").read_text() == "changed"
    assert not (resumed / "gone.md").exists()
    status = (await run_git("status", "--porcelain", cwd=resumed)).split()
    assert status == ["D", "gone.md", "M", "readme.md"]


async def test_a_resumed_thread_keeps_going_from_where_it_was(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(workspace, {"work.md": "first"})
    await manager.save(thread)
    await manager.release(thread.id)

    resumed = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(resumed, {"work.md": "second"})
    await manager.save(thread)

    ref = workspaces.thread_ref(thread.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "second"
    # Two turns, two saves, a prune in between, and the repository the agent works
    # in has exactly the one commit its mirror was forked with.
    assert await commit_count(resumed) == 1


async def test_another_threads_work_is_no_branch_of_this_ones(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # Thread refs live outside `refs/heads/`, so a fork made after a hundred
    # threads have saved work still opens on the project's branches alone.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    one = await a_bound_thread(manager)
    other = await a_bound_thread(manager)
    first = await manager.materialize(a_workspace(manager, one.id), mirror)
    await a_turn(first, {"mine.md": "one"})
    await manager.save(one)

    second = await manager.materialize(a_workspace(manager, other.id), mirror)

    branches = await run_git("branch", "--format=%(refname:short)", cwd=second)
    assert sorted(branches.split()) == sorted([f"octomate/thread-{other.id}", "main"])
    assert not (second / "mine.md").exists()


async def test_snapshots_carry_the_machine_identity(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(workspace, {"work.md": "done"})

    await manager.save(thread)

    ref = workspaces.thread_ref(thread.id)
    author = await run_git("show", "-s", "--format=%an <%ae>", ref, cwd=mirror)
    assert author.strip() == "octomate <octomate@example.com>"


async def test_snapshots_carry_the_configured_identity(tmp_path: Path) -> None:
    # The same identity the mirrors block gives a sync commit: one machine, one
    # name on everything Octomate writes for itself.
    manager = await a_manager(
        tmp_path, identity=GitIdentity(name="Lu Hui", email="lu@example.com")
    )
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    await a_turn(
        await manager.materialize(a_workspace(manager, thread.id), mirror),
        {"work.md": "done"},
    )

    await manager.save(thread)

    ref = workspaces.thread_ref(thread.id)
    author = await run_git("show", "-s", "--format=%an <%ae>", ref, cwd=mirror)
    assert author.strip() == "Lu Hui <lu@example.com>"


async def test_nothing_a_turn_saves_reaches_the_upstream(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # The mirror is the only push target, and the workspace's `origin` is the
    # upstream — so this is what holds a turn to pushing by the mirror's path
    # rather than through whatever remote the fork is pointing at. What a thread
    # did reaches GitHub when someone asks for it, never as a side effect.
    upstream = await a_mirror(tmp_path / "upstream", {"readme.md": "hello"})
    project = manager.projects.get("inky")
    assert project is not None
    mirror = manager.mirrors.path(project)
    await run_git("clone", str(upstream), str(mirror))
    thread = await a_bound_thread(manager)
    await a_turn(
        await manager.materialize(a_workspace(manager, thread.id), mirror),
        {"work.md": "done"},
    )

    await manager.save(thread)

    assert await saved(mirror, thread.id)
    assert not await saved(upstream, thread.id)


async def test_a_workspace_the_mirror_already_has_is_pruned(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(workspace, {"work.md": "done"})
    await manager.save(thread)

    assert await manager.prune(idle=0.0) == [thread.id]

    assert not workspace.exists()
    # And what it held is still the thread's, waiting in the mirror.
    ref = workspaces.thread_ref(thread.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "done"


async def test_a_workspace_in_use_is_left_alone(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # Idleness is measured from the turn that last asked for the workspace, not
    # from the last one that changed a file in it.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await manager.save(thread)

    assert manager.existing(thread.id) == workspace
    assert await manager.prune(idle=3600.0) == []

    assert workspace.exists()


async def test_work_the_mirror_does_not_have_survives_the_sweep(
    manager: WorkspaceManager, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The rule the whole unit rests on: reclaiming a workspace may cost a resume
    # and may never cost the work.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await a_turn(workspace, {"unsaved.md": "never pushed"})

    with caplog.at_level(logging.WARNING):
        assert await manager.prune(idle=0.0) == []

    assert (workspace / "unsaved.md").exists()
    assert "does not have" in caplog.text


async def test_a_commit_the_mirror_has_not_seen_survives_the_sweep(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # What a dirty-tree check would miss entirely: the agent committed its own
    # work, so the workspace is clean and reads as untouched, while what it sits
    # on is a commit no push ever carried. Comparing snapshots is what sees it.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await manager.save(thread)
    await a_turn(workspace, {"work.md": "done"})
    await run_git("add", "-A", cwd=workspace)
    await run_git(*IDENTITY, "commit", "-m", "the agent's own", cwd=workspace)

    assert await run_git("status", "--porcelain", cwd=workspace) == ""
    assert await manager.prune(idle=0.0) == []

    assert workspace.exists()


async def test_a_fork_points_at_the_projects_upstream(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # What an agent means by pushing is the repository people will read it in, not
    # the mirror it happens to have been forked from — and not something that
    # differs by host, which is what inheriting the fork mechanism's answer gives.
    upstream = await a_mirror(tmp_path / "upstream", {"readme.md": "hello"})
    mirror = tmp_path / "mirror"
    await run_git("clone", str(upstream), str(mirror))

    workspace = await manager.materialize(a_workspace(manager, uuid7()), mirror)

    url = await run_git("remote", "get-url", "origin", cwd=workspace)
    assert url.strip() == str(upstream)


async def test_a_fork_of_a_folder_project_has_no_remote(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # A directory upstream is not a push target, so its fork says so by having
    # nowhere to push rather than by failing when someone tries.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})

    workspace = await manager.materialize(a_workspace(manager, uuid7()), mirror)

    assert await run_git("remote", cwd=workspace) == ""


async def test_a_thread_in_no_project_forks_an_empty_workspace(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # OCTO-50: a workspace like any other, through the same `materialize` — forked
    # from the blank mirror rather than a project's, and landing on the thread's own
    # branch exactly as a project fork does. A repository, not a directory: the run
    # may write in it, and what a run does with git works because HEAD resolves.
    thread_id = uuid7()
    workspace = manager.open(thread_id, None)

    assert isinstance(workspace, ChatWorkspace)
    assert (
        workspace.path == (tmp_path / "workspaces" / "chat" / str(thread_id)).resolve()
    )
    # Known before anything is forked, which is what lets a runtime be told where it
    # will run before the process that runs there exists.
    assert not workspace.path.exists()

    async with workspace as chat:
        assert (chat.path / ".git").is_dir()
        branch = await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=chat.path)
        assert branch == f"octomate/thread-{thread_id}\n"
        (chat.path / "scratch.txt").write_text("mine")


async def test_a_chat_workspace_is_not_the_threads_project_workspace(
    manager: WorkspaceManager,
) -> None:
    # The directory's existence is how `prepare` answers "is this thread's workspace
    # already forked", so a chat fork left where a project one goes would be resumed
    # into by the thread's first project turn — which would then run in an empty
    # repository instead of the project's code.
    thread = await a_bound_thread(manager)

    async with manager.open(thread.id, None):
        assert manager.existing(thread.id) is None

    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert (workspace / "readme.md").read_text() == "hello"


async def test_the_sweep_leaves_a_chat_workspace_alone(
    manager: WorkspaceManager,
) -> None:
    # It is its run's to throw away and is kept nowhere the sweep could restore it
    # from, so the pruner must not be the one to decide — and it is not asked,
    # because it prunes by thread id and `chat` is not one.
    async with manager.open(uuid7(), None) as chat:
        assert await manager.prune(idle=0.0) == []
        assert chat.path.is_dir()


async def test_leaving_a_chat_workspace_discards_it(
    manager: WorkspaceManager,
) -> None:
    # Nothing survives the turn: the next run of the same conversation is a fresh
    # empty tree, at the same path — which is what the runtimes key a resumable
    # session by.
    workspace = manager.open(uuid7(), None)

    async with workspace as chat:
        (chat.path / "scratch.txt").write_text("mine")

    assert not workspace.path.exists()

    async with manager.open(workspace.thread_id, None) as second:
        assert second.path == workspace.path
        assert not (second.path / "scratch.txt").exists()


async def test_leaving_a_chat_workspace_a_later_run_is_in_keeps_it(
    manager: WorkspaceManager,
) -> None:
    # A mid-run follow-up starts its turn before the turn it supersedes has finished
    # losing its own. Both are in this tree, and the first to leave must not take
    # the floor out from under the second.
    thread_id = uuid7()
    superseded = manager.open(thread_id, None)
    follow_up = manager.open(thread_id, None)

    async with superseded:
        await follow_up.prepare()
        assert follow_up.path == superseded.path

    assert follow_up.path.is_dir()

    await manager.discard(follow_up)

    assert not follow_up.path.exists()


async def test_the_next_run_throws_away_what_a_killed_one_left(
    manager: WorkspaceManager,
) -> None:
    # Nothing ever resumes into a chat workspace, so a fork that died half-made is
    # reclaimed rather than resumed into — which is why claiming one clears it, and
    # why it needs no staging directory to be forked through.
    workspace = manager.open(uuid7(), None)
    workspace.path.mkdir(parents=True)
    (workspace.path / "half.txt").write_text("from a run nobody finished")

    async with workspace as chat:
        assert not (chat.path / "half.txt").exists()
        assert (chat.path / ".git").is_dir()


async def test_a_chat_run_leaves_nothing_in_the_blank_mirror(
    manager: WorkspaceManager,
) -> None:
    # The mirror is what makes a project workspace disposable; this one has nothing
    # to be disposable about. A ref per conversation for work nobody intends to keep
    # is the opposite of what a mirror is for.
    thread = await ThreadManager(users=UserManager()).ensure(
        ThreadKey("test", "thread", "chat", str(uuid7()))
    )

    async with manager.open(thread.id, None) as chat:
        (chat.path / "scratch.txt").write_text("mine")
        await manager.save(thread)

    blank = await manager.mirrors.create()
    refs = await run_git("for-each-ref", "--format=%(refname)", cwd=blank)
    # Its own empty commit and nothing else — no thread ref, no snapshot.
    assert refs == "refs/heads/main\n"


async def test_a_chat_workspace_goes_even_when_the_run_raised(
    manager: WorkspaceManager,
) -> None:
    # The files an interrupted run wrote are exactly the files nothing will ever ask
    # for again, and a failed turn on a thread nobody resumes would otherwise hold
    # its disk for good.
    workspace = manager.open(uuid7(), None)

    with suppress(RuntimeError):
        async with workspace as chat:
            (chat.path / "scratch.txt").write_text("mine")
            raise RuntimeError("the provider dropped the connection")

    assert not workspace.path.exists()


async def test_leaving_a_project_workspace_keeps_it(
    manager: WorkspaceManager,
) -> None:
    # The other variant's whole ending: a project's workspace outlives the run that
    # used it, which is what makes the next turn a resume.
    thread = await a_bound_thread(manager)
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    workspace = a_workspace(manager, thread.id)
    await manager.materialize(workspace, mirror)

    # Entering one that is already there is a resume, not a fork: the mirror is not
    # synced for it, and the tree is answered as the last turn left it.
    async with workspace:
        (workspace.path / "work.md").write_text("done")

    assert (workspace.path / "work.md").read_text() == "done"
    assert (workspace.path / "readme.md").read_text() == "hello"


async def test_a_fresh_fork_is_not_born_idle(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # `cp -a` preserving the mirror's timestamps is the point of it — a copied
    # index stays valid — but it means the fork inherits the mirror's age too, and
    # a mirror synced a week ago would hand every new workspace to the next sweep.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    long_ago = time.time() - 30 * 24 * 60 * 60
    os.utime(mirror, (long_ago, long_ago))
    thread = await a_bound_thread(manager)

    await manager.materialize(a_workspace(manager, thread.id), mirror)
    await manager.save(thread)

    assert await manager.prune(idle=3600.0) == []


async def test_a_resumed_workspace_can_be_pruned_again(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    # What the last push carried is recorded in the workspace, and a resumed
    # thread's workspace is not the one that recorded it. Without the resume
    # saying so, a thread that was resumed and then went quiet would hold its disk
    # until it next ran, on the strength of work the mirror already has.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    await a_turn(
        await manager.materialize(a_workspace(manager, thread.id), mirror),
        {"work.md": "done"},
    )
    await manager.save(thread)
    await manager.release(thread.id)

    resumed = await manager.materialize(a_workspace(manager, thread.id), mirror)

    assert await manager.saved(resumed)
    assert await manager.prune(idle=0.0) == [thread.id]


async def test_a_fork_in_progress_is_not_a_workspace_to_prune(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await manager.save(thread)
    staging = workspace.with_name(f".{uuid7()}.forking")
    staging.mkdir()

    assert await manager.prune(idle=0.0) == [thread.id]

    assert staging.exists()


async def test_the_sweep_prunes_on_its_interval(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(a_workspace(manager, thread.id), mirror)
    await manager.save(thread)
    manager.config = WorkspacesConfig(idle_window=0.001, sweep_interval=0.001)

    sweeping = asyncio.create_task(manager.sweep())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not workspace.exists():
            break
    sweeping.cancel()
    with suppress(asyncio.CancelledError):
        await sweeping

    assert not workspace.exists()


async def test_a_named_ref_is_where_the_workspace_starts(
    manager: WorkspaceManager,
) -> None:
    # OCTO-52: the default branch is the wrong answer often enough — continuing
    # someone's feature branch, reproducing against a tag, working from a PR head.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    await run_git("checkout", "-b", "feat/theirs", cwd=mirror)
    (mirror / "theirs.md").write_text("wip")
    await run_git("add", "-A", cwd=mirror)
    await run_git(*IDENTITY, "commit", "-m", "theirs", cwd=mirror)
    await run_git("checkout", "main", cwd=mirror)
    thread = await a_bound_thread(manager)

    workspace = await manager.materialize(
        a_workspace(manager, thread.id), mirror, "feat/theirs"
    )

    assert (workspace / "theirs.md").read_text() == "wip"
    # Started there, but still on the thread's own branch: where the work begins
    # and whose branch it lands on are separate questions.
    assert await branch(workspace) == f"octomate/thread-{thread.id}"


async def test_a_ref_the_mirror_does_not_have_fails_the_fork(
    manager: WorkspaceManager,
) -> None:
    # A starting point that does not resolve has to fail where it was asked for.
    # Falling back to the default branch would be a thread quietly doing its work
    # somewhere nobody chose.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)

    with pytest.raises(GitCommandError):
        await manager.materialize(
            a_workspace(manager, thread.id), mirror, "no/such/ref"
        )

    # And the half-made fork is gone, so the next turn forks again rather than
    # reading a staging directory as this thread's workspace.
    assert manager.existing(thread.id) is None
