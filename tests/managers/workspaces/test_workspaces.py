"""A thread's workspace is a tree volume in its owner's root: a fork of the
project's mirror, and a disposable one.

Each fork is an independent repository containing project history and only its
own saved snapshot. Peer objects must be absent even when their hashes are known.
Saving to the mirror makes the working directory disposable without losing work.

A snapshot is not a commit on the agent's branch, and the tests below are mostly
about the difference: the repository comes back as the agent left it, history and
uncommitted mess alike, with nothing added to it on its behalf.

The rows are real: a tree's volume names its owner, thread and project by foreign
key, and `save` resolves where to push through the thread's project.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.lowlevel import checkpoint
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.mirrors import GitIdentity, MirrorsConfig
from octomate.config.workspaces import WorkspacesConfig
from octomate.database import async_session
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import snapshots
from octomate.managers.workspaces.base import (
    ChatWorkspace,
    ProjectWorkspace,
    UnregisteredProjectError,
    UnregisteredWorkspace,
    WorkspaceManager,
    tree_ref,
)
from octomate.managers.workspaces.dependencies import install
from octomate.managers.workspaces.mirrors import (
    GitCommandError,
    MirrorManager,
    run_git,
)
from octomate.nerve import Nerve
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.user import User
from octomate.schemas.volumes import TreeVolume
from tests.support.dependencies import Probe, probing, runs
from tests.support.managers import a_project, a_registry, a_thread
from tests.support.users import a_user

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


@pytest.fixture
async def owner(in_memory_engine: AsyncEngine) -> User:
    """The registered user whose root the trees land in."""
    return await a_user()


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
        volumes_dir=tmp_path / "volumes",
    )


async def a_bound_thread(manager: WorkspaceManager) -> Thread:
    """A thread of the one kind that carries a project, filed under the registered
    one. `save` finds the mirror to push to through this."""
    return await ThreadManager(nerve=Nerve(), users=UserManager()).ensure(
        ThreadKey("test", "thread", "chat", str(uuid7())),
        project=manager.projects.get("inky"),
    )


async def a_chat_thread() -> Thread:
    """A thread in no project."""
    return await ThreadManager(nerve=Nerve(), users=UserManager()).ensure(
        ThreadKey("test", "thread", "chat", str(uuid7()))
    )


async def a_workspace(
    manager: WorkspaceManager, thread: Thread, owner: User, ref: str | None = None
) -> ProjectWorkspace:
    """The handle a run is given before anything is forked: its row is written,
    its folder is not."""
    project = manager.projects.get("inky")
    assert project is not None
    return await manager.resolve(thread.id, project, owner.id, ref)


async def a_tree(manager: WorkspaceManager, owner: User, mirror: Path) -> Path:
    """A forked tree on a fresh bound thread."""
    thread = await a_bound_thread(manager)
    return await manager.materialize(await a_workspace(manager, thread, owner), mirror)


async def a_project_mirror(manager: WorkspaceManager, files: dict[str, str]) -> Path:
    """The mirror `MirrorManager.sync` would leave for the registered project,
    built here directly so this unit turns on nothing but git."""
    project = manager.projects.get("inky")
    assert project is not None
    return await a_mirror(manager.mirrors.path(project), files)


async def age(volume: TreeVolume, seconds: float) -> None:
    """Move a tree's last use `seconds` into the past, as a quiet thread would."""
    async with async_session() as session:
        row = await session.one_or_none(
            TreeVolume, expressions=[TreeVolume["id"] == volume.id]
        )
        assert row is not None
        row.last_used_at = datetime.now(UTC) - timedelta(seconds=seconds)
        await session.commit()


@pytest.fixture
async def manager(tmp_path: Path) -> WorkspaceManager:
    return await a_manager(tmp_path)


async def a_mirror(path: Path, files: dict[str, str]) -> Path:
    """A pristine mirror to fork from — what `MirrorManager.sync` leaves behind,
    built here directly so this unit's tests turn on nothing but git."""
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
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
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)

    workspace = await manager.materialize(opened, mirror)

    assert (
        workspace
        == (
            tmp_path / "volumes" / str(owner.id) / "tree" / str(opened.volume.id)
        ).resolve()
    )
    assert (workspace / "readme.md").read_text() == "hello"
    assert await branch(workspace) == f"octomate/thread-{thread.id}"
    assert await common_dir(workspace) == workspace / ".git"


async def test_a_commit_in_a_workspace_writes_only_inside_it(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    before = sorted(path.relative_to(mirror) for path in (mirror / ".git").rglob("*"))
    workspace = await a_tree(manager, owner, mirror)

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
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    one = await a_bound_thread(manager)
    other = await a_bound_thread(manager)

    first = await manager.materialize(await a_workspace(manager, one, owner), mirror)
    second = await manager.materialize(await a_workspace(manager, other, owner), mirror)

    assert first != second
    (first / "mine.md").write_text("only here")
    assert not (second / "mine.md").exists()
    assert await branch(second) == f"octomate/thread-{other.id}"


async def test_two_users_in_one_thread_get_their_own_trees_and_refs(
    manager: WorkspaceManager, owner: User
) -> None:
    # A shared thread is one conversation and two people's work: neither may see
    # the other's uncommitted files, and neither's save may land on the other's ref.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    other = await a_user("bob")
    thread = await a_bound_thread(manager)
    mine = await a_workspace(manager, thread, owner)
    theirs = await a_workspace(manager, thread, other)
    await a_turn(await manager.materialize(mine, mirror), {"work.md": "mine"})
    await manager.materialize(theirs, mirror)

    await manager.save(thread, owner.id)

    assert mine.volume.id != theirs.volume.id
    assert mine.path != theirs.path
    assert not (theirs.path / "work.md").exists()
    assert await saved(mirror, mine.volume)
    assert not await saved(mirror, theirs.volume)

    await a_turn(theirs.path, {"work.md": "theirs"})
    await manager.save(thread, other.id)

    show = ("show", f"{tree_ref(mine.volume.id)}:work.md")
    assert await run_git(*show, cwd=mirror) == "mine"
    show = ("show", f"{tree_ref(theirs.volume.id)}:work.md")
    assert await run_git(*show, cwd=mirror) == "theirs"


async def test_materializing_again_answers_the_live_tree(
    manager: WorkspaceManager, owner: User
) -> None:
    # The turn after the first one asks for the same workspace, and the work in it
    # is uncommitted more often than not.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(
        await a_workspace(manager, thread, owner), mirror
    )
    (workspace / "wip.md").write_text("half done")

    again = await a_workspace(manager, thread, owner)
    assert await manager.materialize(again, mirror) == workspace
    assert (workspace / "wip.md").read_text() == "half done"


async def test_releasing_removes_the_workspace_and_leaves_the_mirror(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    (workspace / "work.md").write_text("done")

    await manager.release(opened.volume)

    assert not workspace.exists()
    assert (mirror / "readme.md").read_text() == "hello"
    assert await run_git("status", "--porcelain", cwd=mirror) == ""


async def test_releasing_keeps_the_row_so_the_tree_comes_back_at_its_path(
    manager: WorkspaceManager, owner: User
) -> None:
    # A harness keys a resumable session by its working directory, so a tree that
    # the sweep took back has to come back where it was.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await manager.materialize(opened, mirror)
    await manager.save(thread, owner.id)

    await manager.release(opened.volume)
    reopened = await a_workspace(manager, thread, owner)

    assert not opened.path.exists()
    assert reopened.volume.id == opened.volume.id
    assert reopened.path == opened.path
    async with async_session() as session:
        rows = await session.list(
            TreeVolume, limit=None, expressions=[TreeVolume["thread_id"] == thread.id]
        )
    assert [row.id for row in rows] == [opened.volume.id]


async def test_releasing_a_workspace_that_is_not_there_is_no_work(
    manager: WorkspaceManager, owner: User
) -> None:
    # Pruning a tree whose folder is gone, and releasing one twice, both arrive here.
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)

    await manager.release(opened.volume)


async def test_a_fork_that_fails_leaves_nothing_behind(
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)

    with pytest.raises(FileNotFoundError):
        await manager.materialize(opened, tmp_path / "no-such-mirror")

    assert not opened.path.exists()
    assert list(opened.path.parent.iterdir()) == []


async def test_what_a_killed_fork_left_is_never_a_workspace(
    manager: WorkspaceManager, owner: User
) -> None:
    # The case the cleanup cannot reach: a machine that dies mid-copy leaves the
    # half-copied tree behind. It is not where the workspace goes, and the next
    # fork of this volume reclaims it rather than building on it.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    abandoned = opened.path.with_name(f".{opened.volume.id}.forking")
    abandoned.mkdir(parents=True)
    (abandoned / "half.md").write_text("copied before the power went")

    workspace = await manager.materialize(opened, mirror)

    assert (workspace / "readme.md").read_text() == "hello"
    assert not (workspace / "half.md").exists()
    assert not abandoned.exists()


async def test_two_materializations_of_one_tree_serialize(
    manager: WorkspaceManager, owner: User
) -> None:
    # Two turns arriving together must not have two `cp`s racing into one path.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    await manager.materialize(opened, mirror)

    lock = manager.locks[opened.volume.id]
    await lock.acquire()
    second = asyncio.create_task(manager.materialize(opened, mirror))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()

    lock.release()
    assert await second == opened.path


async def test_a_workspace_path_is_absolute_from_a_relative_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default `.octomate/volumes` is relative to wherever Octomate was
    # started, and a tree is handed to processes started elsewhere.
    monkeypatch.chdir(tmp_path)
    manager = WorkspaceManager()
    volume = TreeVolume(owner_id=uuid7(), thread_id=uuid7())

    assert manager.path(volume) == (
        tmp_path / ".octomate/volumes" / str(volume.owner_id) / "tree" / str(volume.id)
    )


async def test_the_sweep_works_from_the_default_relative_directory(
    tmp_path: Path, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every other test here names an absolute directory, which is the one shape
    # production never uses: `.octomate/volumes` hangs off wherever Octomate was
    # started. A relative root reaches git as a relative `GIT_INDEX_FILE` against
    # a tree's cwd, and resolves inside the tree it describes.
    monkeypatch.chdir(tmp_path)
    manager = WorkspaceManager(
        projects=await a_registry(a_project(a_root(tmp_path))),
        mirrors=MirrorManager(mirrors_dir=Path(".octomate/mirrors")),
    )
    thread = await a_bound_thread(manager)
    project = manager.projects.get("inky")
    assert project is not None
    mirror = await a_mirror(manager.mirrors.path(project), {"readme.md": "hello"})
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "done"})
    await manager.save(thread, owner.id)

    assert await manager.prune(idle=0.0) == [opened.volume.id]


async def a_turn(workspace: Path, files: dict[str, str]) -> None:
    """What a turn leaves on disk — the workspace's own writes, uncommitted, as an
    agent leaves them when its run ends."""
    for name, content in files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(content)


async def saved(mirror: Path, volume: TreeVolume) -> str:
    """What the mirror is keeping for this tree, or "" when it keeps nothing."""
    return (await run_git("ls-remote", str(mirror), tree_ref(volume.id))).strip()


async def test_a_turn_leaves_its_work_on_the_trees_ref(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "done"})

    await manager.save(thread, owner.id)

    ref = tree_ref(opened.volume.id)
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
    manager: WorkspaceManager, owner: User
) -> None:
    # The whole reason a turn is snapshotted rather than committed: what the agent
    # did with git is the agent's, and saving its work is not an edit to it. A
    # commit here would land on the agent's branch, and the next turn's commit
    # would bury it in a history a person is going to read.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(
        await a_workspace(manager, thread, owner), mirror
    )
    await their_own_branch(workspace)
    before = await head(workspace)

    await manager.save(thread, owner.id)

    assert await head(workspace) == before
    assert await branch(workspace) == "feat/theirs"
    subjects = await run_git("log", "--format=%s", cwd=workspace)
    assert subjects.splitlines() == ["feat: real work", "mirror"]
    assert (await run_git("status", "--porcelain", cwd=workspace)).split() == [
        "??",
        "wip.md",
    ]


async def test_a_workspace_comes_back_on_the_branch_the_agent_made(
    manager: WorkspaceManager, owner: User
) -> None:
    # And with the agent's own commit under it, rather than on a thread branch it
    # never chose. Resolving that history is the agent's job; the resume's job is
    # to hand back the repository it was working in.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    await their_own_branch(workspace)
    await manager.save(thread, owner.id)
    left = await head(workspace)

    await manager.release(opened.volume)
    resumed = await manager.materialize(opened, mirror)

    assert await branch(resumed) == "feat/theirs"
    assert await head(resumed) == left
    subjects = await run_git("log", "--format=%s", cwd=resumed)
    assert subjects.splitlines() == ["feat: real work", "mirror"]
    assert (resumed / "wip.md").read_text() == "not yet"


async def test_a_detached_head_comes_back_detached(
    manager: WorkspaceManager, owner: User
) -> None:
    # An agent reproducing something against a tag or an older commit is on no
    # branch at all, which is a state to restore rather than one to correct.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    await run_git("checkout", "--detach", "HEAD", cwd=workspace)
    await a_turn(workspace, {"probe.md": "why does it fail here"})
    await manager.save(thread, owner.id)
    left = await head(workspace)

    await manager.release(opened.volume)
    resumed = await manager.materialize(opened, mirror)

    assert await branch(resumed) == "HEAD"
    assert await head(resumed) == left
    assert (resumed / "probe.md").read_text() == "why does it fail here"


async def test_a_turn_that_changed_nothing_still_names_the_ref(
    manager: WorkspaceManager, owner: User
) -> None:
    # The push is what a later prune relies on, so it happens whether or not the
    # turn had anything to commit.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)

    await manager.save(thread, owner.id)

    assert await saved(mirror, opened.volume)
    assert await commit_count(workspace) == 1


async def test_a_released_workspace_comes_back_with_its_work(
    manager: WorkspaceManager, owner: User
) -> None:
    # Pruning is a disk decision: the thread resumes with the tree its last turn
    # left, on the same branch, from a fork that was made all over again — and
    # work the agent had not committed comes back uncommitted, since restoring it
    # any other way would be handing the agent a state it never made.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    await a_turn(workspace, {"work.md": "done", "deep/nested.md": "also"})
    await manager.save(thread, owner.id)
    left = await head(workspace)

    await manager.release(opened.volume)
    resumed = await manager.materialize(opened, mirror)

    assert (resumed / "work.md").read_text() == "done"
    assert (resumed / "deep" / "nested.md").read_text() == "also"
    assert await head(resumed) == left
    assert await branch(resumed) == f"octomate/thread-{thread.id}"
    assert sorted(
        (await run_git("status", "--porcelain", cwd=resumed)).split()
    ) == sorted(["??", "deep/", "??", "work.md"])


async def test_a_modified_file_comes_back_modified(
    manager: WorkspaceManager, owner: User
) -> None:
    # And a deleted one comes back deleted. The snapshot is the tree as it stood,
    # so a resume restores the diff the agent was looking at rather than a fork
    # that merely contains its files.
    mirror = await a_project_mirror(
        manager, {"readme.md": "hello", "gone.md": "not for long"}
    )
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    (workspace / "readme.md").write_text("changed")
    (workspace / "gone.md").unlink()
    await manager.save(thread, owner.id)

    await manager.release(opened.volume)
    resumed = await manager.materialize(opened, mirror)

    assert (resumed / "readme.md").read_text() == "changed"
    assert not (resumed / "gone.md").exists()
    status = (await run_git("status", "--porcelain", cwd=resumed)).split()
    assert status == ["D", "gone.md", "M", "readme.md"]


async def test_a_resumed_thread_keeps_going_from_where_it_was(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "first"})
    await manager.save(thread, owner.id)
    await manager.release(opened.volume)

    resumed = await manager.materialize(opened, mirror)
    await a_turn(resumed, {"work.md": "second"})
    await manager.save(thread, owner.id)

    ref = tree_ref(opened.volume.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "second"
    # Two turns, two saves, a prune in between, and the repository the agent works
    # in has exactly the one commit its mirror was forked with.
    assert await commit_count(resumed) == 1


async def test_another_threads_work_is_no_branch_of_this_ones(
    manager: WorkspaceManager, owner: User
) -> None:
    # Tree refs live outside `refs/heads/`, so a fork made after a hundred trees
    # have saved work still opens on the project's branches alone.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    one = await a_bound_thread(manager)
    other = await a_bound_thread(manager)
    first = await manager.materialize(await a_workspace(manager, one, owner), mirror)
    await a_turn(first, {"mine.md": "one"})
    await manager.save(one, owner.id)

    second = await manager.materialize(await a_workspace(manager, other, owner), mirror)

    branches = await run_git("branch", "--format=%(refname:short)", cwd=second)
    assert sorted(branches.split()) == sorted([f"octomate/thread-{other.id}", "main"])
    assert not (second / "mine.md").exists()


async def test_another_threads_snapshot_is_no_ref_of_this_ones(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    one = await a_bound_thread(manager)
    other = await a_bound_thread(manager)
    first = await manager.materialize(await a_workspace(manager, one, owner), mirror)
    await a_turn(first, {"secret.md": "one"})
    await manager.save(one, owner.id)

    second = await manager.materialize(await a_workspace(manager, other, owner), mirror)

    refs = await run_git("for-each-ref", "--format=%(refname)", "refs/", cwd=second)
    assert [ref for ref in refs.split() if ref.startswith("refs/octomate/")] == []
    reachable = await run_git("log", "--all", "--format=%s", cwd=second)
    assert f"octomate/thread-{one.id}" not in reachable


@pytest.mark.parametrize("packed", [False, True])
async def test_peer_snapshot_objects_are_absent_even_by_hash(
    manager: WorkspaceManager, owner: User, packed: bool
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    mine = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(mine, mirror), {"secret.md": "private"})
    await manager.save(thread, owner.id)
    private = (await run_git("rev-parse", tree_ref(mine.volume.id), cwd=mirror)).strip()
    blob = (await run_git("rev-parse", f"{private}:secret.md", cwd=mirror)).strip()
    if packed:
        await run_git("repack", "-ad", cwd=mirror)

    peer = await a_workspace(manager, thread, await a_user("bob"))
    path = await manager.materialize(peer, mirror)

    for oid in (private, blob):
        with pytest.raises(GitCommandError):
            await run_git("cat-file", "-e", oid, cwd=path)
    assert not (path / ".git/objects/info/alternates").exists()
    assert all(
        file.stat().st_nlink == 1
        for file in (path / ".git/objects").rglob("*")
        if file.is_file()
    )
    await a_turn(path, {"bob.md": "bob's private work"})
    await manager.save(thread, peer.owner_id)
    peer_snapshot = (
        await run_git("rev-parse", tree_ref(peer.volume.id), cwd=mirror)
    ).strip()
    await manager.release(mine.volume)

    restored = await manager.materialize(mine, mirror)

    assert (restored / "secret.md").read_text() == "private"
    assert not (restored / "bob.md").exists()
    assert await manager.saved(mine.volume)
    with pytest.raises(GitCommandError):
        await run_git("cat-file", "-e", peer_snapshot, cwd=restored)


async def test_unreachable_objects_are_not_exported(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    await run_git("checkout", "-b", "temporary", cwd=mirror)
    (mirror / "deleted-secret.md").write_text("unreachable")
    await run_git("add", "-A", cwd=mirror)
    await run_git(*IDENTITY, "commit", "-m", "unreachable", cwd=mirror)
    unreachable = await head(mirror)
    await run_git("checkout", "main", cwd=mirror)
    await run_git("branch", "-D", "temporary", cwd=mirror)
    await run_git("repack", "-ad", cwd=mirror)

    path = await a_tree(manager, owner, mirror)

    with pytest.raises(GitCommandError):
        await run_git("cat-file", "-e", unreachable, cwd=path)


async def test_mirror_replace_refs_cannot_export_private_history(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    mine = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(mine, mirror), {"secret.md": "private"})
    await manager.save(thread, owner.id)
    private = (await run_git("rev-parse", tree_ref(mine.volume.id), cwd=mirror)).strip()
    await run_git("replace", "HEAD", private, cwd=mirror)

    peer = await a_workspace(manager, thread, await a_user("bob"))
    path = await manager.materialize(peer, mirror)

    assert not (path / "secret.md").exists()
    assert (path / "readme.md").read_text() == "hello"
    with pytest.raises(GitCommandError):
        await run_git("cat-file", "-e", private, cwd=path)


@pytest.mark.parametrize("ref", ["feature", "v1", "origin/feature", "commit"])
async def test_authorized_project_refs_can_start_a_workspace(
    manager: WorkspaceManager, owner: User, ref: str
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    await run_git("checkout", "-b", "feature", cwd=mirror)
    (mirror / "feature.md").write_text("public history")
    await run_git("add", "-A", cwd=mirror)
    await run_git(*IDENTITY, "commit", "-m", "feature", cwd=mirror)
    commit = await head(mirror)
    await run_git(*IDENTITY, "tag", "-a", "v1", "-m", "release", cwd=mirror)
    await run_git("update-ref", "refs/remotes/origin/feature", commit, cwd=mirror)
    await run_git("checkout", "main", cwd=mirror)
    if ref != "feature":
        await run_git("branch", "-D", "feature", cwd=mirror)
    opened = await a_workspace(
        manager,
        await a_bound_thread(manager),
        owner,
        commit if ref == "commit" else ref,
    )

    path = await manager.materialize(opened, mirror)

    assert await head(path) == commit
    assert (path / "feature.md").read_text() == "public history"
    assert await run_git("cat-file", "-t", "refs/tags/v1", cwd=path) == "tag\n"


@pytest.mark.parametrize("by_hash", [False, True])
async def test_base_ref_cannot_request_a_peer_snapshot(
    manager: WorkspaceManager, owner: User, by_hash: bool
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    mine = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(mine, mirror), {"secret.md": "private"})
    await manager.save(thread, owner.id)
    ref = tree_ref(mine.volume.id)
    if by_hash:
        ref = (await run_git("rev-parse", ref, cwd=mirror)).strip()
    peer = await a_workspace(manager, thread, await a_user("bob"), ref)

    with pytest.raises(GitCommandError):
        await manager.materialize(peer, mirror)
    assert not peer.path.exists()


async def test_fork_does_not_copy_mirror_metadata_or_untracked_files(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    (mirror / ".env").write_text("HOST_SECRET=private")
    (mirror / ".git/host-secret").write_text("private")
    (mirror / ".git/hooks/post-checkout").write_text("host hook")
    await run_git("config", "credential.helper", "host-only", cwd=mirror)

    path = await a_tree(manager, owner, mirror)

    assert not (path / ".env").exists()
    assert not (path / ".git/host-secret").exists()
    assert not (path / ".git/hooks/post-checkout").exists()
    with pytest.raises(GitCommandError):
        await run_git("config", "--local", "--get", "credential.helper", cwd=path)


@pytest.mark.parametrize(
    "trigger", ["filter", "fsmonitor", "pre-push", "reference-transaction"]
)
async def test_saving_never_runs_workspace_git_commands(
    manager: WorkspaceManager, owner: User, tmp_path: Path, trigger: str
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    (path / "work.txt").write_text("unfinished")
    marker = tmp_path / "executed"
    command = path / ".git" / "attack"
    command.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\n')
    command.chmod(0o755)
    if trigger == "filter":
        (path / ".gitattributes").write_text("*.txt filter=attack\n")
        await run_git("config", "filter.attack.clean", str(command), cwd=path)
    elif trigger == "fsmonitor":
        await run_git("config", "core.fsmonitor", str(command), cwd=path)
    else:
        hooks = path / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        command.rename(hooks / trigger)

    await manager.checkpoint(opened)

    assert not marker.exists()
    assert (
        await run_git("show", f"{tree_ref(opened.volume.id)}:work.txt", cwd=mirror)
        == "unfinished"
    )


@pytest.mark.parametrize(
    "entry", ["HEAD", "index", "objects", "objects/info/alternates"]
)
async def test_redirected_git_data_cannot_be_saved_or_pruned(
    manager: WorkspaceManager, owner: User, tmp_path: Path, entry: str
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    (path / "work.txt").write_text("unfinished")
    target = path / ".git" / entry
    outside = tmp_path / "outside"
    if entry.endswith("alternates"):
        target.parent.mkdir(exist_ok=True)
        target.write_text(str(mirror / ".git/objects") + "\n")
    else:
        target.rename(outside)
        target.symlink_to(outside)

    await manager.checkpoint(opened)

    assert not await saved(mirror, opened.volume)
    assert await manager.prune(idle=0.0) == []
    assert (path / "work.txt").read_text() == "unfinished"


async def test_a_forged_saved_marker_cannot_discard_unimported_work(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    (path / "work.txt").write_text("only in this tree")
    await run_git("add", "-A", cwd=path)
    tree = (await run_git("write-tree", cwd=path)).strip()
    forged = (
        await run_git(
            *IDENTITY,
            "commit-tree",
            tree,
            "-p",
            "HEAD",
            "-m",
            f"octomate: {await branch(path)}",
            cwd=path,
        )
    ).strip()
    await run_git("update-ref", "refs/octomate/saved", forged, cwd=path)

    assert await manager.prune(idle=0.0) == []
    assert (path / "work.txt").read_text() == "only in this tree"
    assert not await saved(mirror, opened.volume)


async def test_capture_preserves_staged_files_and_records_symlinks_without_following(
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(
        manager, {"readme.md": "hello", ".gitignore": "*.local\n"}
    )
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    (path / "staged.local").write_text("staged")
    await run_git("add", "-f", "staged.local", cwd=path)
    original_index = (path / ".git/index").read_bytes()
    original_head = await head(path)
    (path / "ignored.local").write_text("ignored")
    outside = tmp_path / "outside.txt"
    outside.write_text("host-only data")
    (path / "link").symlink_to(outside)

    await manager.checkpoint(opened)

    ref = tree_ref(opened.volume.id)
    assert (path / ".git/index").read_bytes() == original_index
    assert await head(path) == original_head
    assert await run_git("show", f"{ref}:staged.local", cwd=mirror) == "staged"
    assert await run_git("show", f"{ref}:link", cwd=mirror) == str(outside)
    with pytest.raises(GitCommandError):
        await run_git("show", f"{ref}:ignored.local", cwd=mirror)


async def test_capture_ignores_local_config_and_inherited_git_environment(
    manager: WorkspaceManager,
    owner: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    (path / "work.txt").write_text("unfinished")
    (path / ".git/config").write_text("deliberately invalid config")
    redirected = tmp_path / "redirected"
    with monkeypatch.context() as injected:
        injected.setenv("GIT_DIR", str(redirected))
        injected.setenv("GIT_CONFIG_COUNT", "1")
        injected.setenv("GIT_CONFIG_KEY_0", "core.worktree")
        injected.setenv("GIT_CONFIG_VALUE_0", str(redirected))
        await manager.checkpoint(opened)

    assert not redirected.exists()
    assert (
        await run_git("show", f"{tree_ref(opened.volume.id)}:work.txt", cwd=mirror)
        == "unfinished"
    )


async def test_corrupt_returned_objects_do_not_replace_the_saved_snapshot(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    await manager.checkpoint(opened)
    original = await saved(mirror, opened.volume)
    (path / "work.txt").write_text("unfinished")
    await run_git("add", "-A", cwd=path)
    await run_git(*IDENTITY, "commit", "-m", "new work", cwd=path)
    commit = await head(path)
    returned = path / ".git/objects" / commit[:2] / commit[2:]
    returned.chmod(0o644)
    returned.write_bytes(b"corrupt object")

    await manager.checkpoint(opened)

    assert await saved(mirror, opened.volume) == original
    assert await manager.prune(idle=0.0) == []
    assert (path / "work.txt").read_text() == "unfinished"


async def test_restoring_a_snapshot_does_not_run_host_filters(
    manager: WorkspaceManager,
    owner: User,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(manager, await a_bound_thread(manager), owner)
    path = await manager.materialize(opened, mirror)
    (path / ".gitattributes").write_text("*.txt filter=attack\n")
    (path / "work.txt").write_text("unfinished")
    await manager.checkpoint(opened)
    await manager.release(opened.volume)
    marker = tmp_path / "executed"
    command = tmp_path / "attack"
    command.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\ncat\n')
    command.chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f'[filter "attack"]\nsmudge = {command}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    restored = await manager.materialize(opened, mirror)

    assert not marker.exists()
    assert (restored / "work.txt").read_text() == "unfinished"


async def test_carry_preserves_the_owners_uncommitted_work_on_a_new_thread_branch(
    manager: WorkspaceManager, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = await a_project_mirror(
        manager, {"readme.md": "hello", ".gitignore": ".env\n"}
    )
    monkeypatch.setattr(manager.mirrors, "sync", AsyncMock(return_value=mirror))
    source = await a_bound_thread(manager)
    target = await a_bound_thread(manager)
    opened = await a_workspace(manager, source, owner)
    path = await manager.materialize(opened, mirror)
    original_head = await head(path)
    (path / "readme.md").write_text("unfinished")
    (path / ".env").write_text("owner secret")
    peer = await a_user("peer")
    peer_path = await manager.materialize(
        await a_workspace(manager, source, peer), mirror
    )
    (peer_path / ".env").write_text("peer secret")

    await manager.carry(source, target, owner.id)

    landed = await a_workspace(manager, target, owner)
    assert (landed.path / "readme.md").read_text() == "unfinished"
    assert (landed.path / ".env").read_text() == "owner secret"
    assert await head(landed.path) == original_head
    assert await branch(landed.path) == f"octomate/thread-{target.id}"
    assert "readme.md" in await run_git("diff", "--name-only", cwd=landed.path)
    assert (path / "readme.md").read_text() == "unfinished"
    assert (peer_path / ".env").read_text() == "peer secret"


async def test_carry_ignores_source_git_config(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {".gitignore": ".env\n"})
    source = await a_bound_thread(manager)
    target = await a_bound_thread(manager)
    source_path = await manager.materialize(
        await a_workspace(manager, source, owner), mirror
    )
    target_path = await manager.materialize(
        await a_workspace(manager, target, owner), mirror
    )
    (source_path / ".env").write_text("private configuration")
    (source_path / ".git/config").write_text("deliberately invalid config")

    await manager.carry(source, target, owner.id)

    assert (target_path / ".env").read_text() == "private configuration"


async def test_carry_preserves_a_dangling_destination_symlink(
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    mirror = await a_project_mirror(manager, {".gitignore": ".env\n"})
    source = await a_bound_thread(manager)
    target = await a_bound_thread(manager)
    source_path = await manager.materialize(
        await a_workspace(manager, source, owner), mirror
    )
    target_path = await manager.materialize(
        await a_workspace(manager, target, owner), mirror
    )
    (source_path / ".env").write_text("private configuration")
    outside = tmp_path / "outside"
    (target_path / ".env").symlink_to(outside)

    await manager.carry(source, target, owner.id)

    assert not outside.exists()
    assert (target_path / ".env").readlink() == outside


async def test_snapshots_carry_the_machine_identity(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "done"})

    await manager.save(thread, owner.id)

    ref = tree_ref(opened.volume.id)
    author = await run_git("show", "-s", "--format=%an <%ae>", ref, cwd=mirror)
    assert author.strip() == "octomate <octomate@example.com>"


async def test_snapshots_carry_the_configured_identity(
    tmp_path: Path, owner: User
) -> None:
    # The same identity the mirrors block gives a sync commit: one machine, one
    # name on everything Octomate writes for itself.
    manager = await a_manager(
        tmp_path, identity=GitIdentity(name="Lu Hui", email="lu@example.com")
    )
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "done"})

    await manager.save(thread, owner.id)

    ref = tree_ref(opened.volume.id)
    author = await run_git("show", "-s", "--format=%an <%ae>", ref, cwd=mirror)
    assert author.strip() == "Lu Hui <lu@example.com>"


async def test_nothing_a_turn_saves_reaches_the_upstream(
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    # The mirror is the only push target, and the tree's `origin` is the upstream —
    # so this is what holds a turn to pushing by the mirror's path rather than
    # through whatever remote the fork is pointing at. What a thread did reaches
    # GitHub when someone asks for it, never as a side effect.
    upstream = await a_mirror(tmp_path / "upstream", {"readme.md": "hello"})
    project = manager.projects.get("inky")
    assert project is not None
    mirror = manager.mirrors.path(project)
    await run_git("clone", str(upstream), str(mirror))
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "done"})

    await manager.save(thread, owner.id)

    assert await saved(mirror, opened.volume)
    assert not await saved(upstream, opened.volume)


async def test_a_workspace_the_mirror_already_has_is_pruned(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    await a_turn(workspace, {"work.md": "done"})
    await manager.save(thread, owner.id)

    assert await manager.prune(idle=0.0) == [opened.volume.id]

    assert not workspace.exists()
    # And what it held is still the tree's, waiting in the mirror.
    ref = tree_ref(opened.volume.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "done"


async def test_a_tree_idle_past_the_window_is_released(
    manager: WorkspaceManager, owner: User
) -> None:
    # Idleness is the row's `last_used_at`, not anything about the folder.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await manager.materialize(opened, mirror)
    await manager.save(thread, owner.id)

    await age(opened.volume, 7200.0)

    assert await manager.prune(idle=3600.0) == [opened.volume.id]
    assert not opened.path.exists()


async def test_opening_a_tree_marks_it_used(
    manager: WorkspaceManager, owner: User
) -> None:
    # Idleness is measured from the turn that last asked for the tree, not from
    # the last one that changed a file in it.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await manager.materialize(opened, mirror)
    await manager.save(thread, owner.id)
    await age(opened.volume, 7200.0)

    await a_workspace(manager, thread, owner)

    assert await manager.prune(idle=3600.0) == []
    assert opened.path.exists()


async def test_a_dismissed_workspace_is_saved_and_then_released(
    manager: WorkspaceManager, owner: User
) -> None:
    # The turn's own work goes to the mirror before the tree goes: a `dismiss` call
    # is asked for mid-turn, before the save the turn's end would have done.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    await a_turn(workspace, {"work.md": "done"})

    assert await manager.dismiss(thread, owner.id)

    assert not workspace.exists()
    ref = tree_ref(opened.volume.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "done"


async def test_a_dismiss_whose_save_did_not_take_keeps_the_workspace(
    manager: WorkspaceManager, owner: User, caplog: pytest.LogCaptureFixture
) -> None:
    # The sweep's rule, unbent by being asked: nothing releases work the mirror
    # has not seen.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(
        await a_workspace(manager, thread, owner), mirror
    )
    await a_turn(workspace, {"unsaved.md": "never pushed"})
    shutil.rmtree(mirror)

    with caplog.at_level(logging.WARNING):
        assert not await manager.dismiss(thread, owner.id)

    assert (workspace / "unsaved.md").exists()
    assert "does not have" in caplog.text


async def test_dismissing_a_thread_with_no_workspace_is_no_work(
    manager: WorkspaceManager, owner: User
) -> None:
    assert not await manager.dismiss(await a_bound_thread(manager), owner.id)


async def test_a_workspace_in_use_is_left_alone(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(
        await a_workspace(manager, thread, owner), mirror
    )
    await manager.save(thread, owner.id)

    assert await manager.prune(idle=3600.0) == []

    assert workspace.exists()


async def test_work_the_mirror_does_not_have_survives_the_sweep(
    manager: WorkspaceManager, owner: User, caplog: pytest.LogCaptureFixture
) -> None:
    # The rule the whole unit rests on: reclaiming a tree may cost a resume and
    # may never cost the work.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    workspace = await a_tree(manager, owner, mirror)
    await a_turn(workspace, {"unsaved.md": "never pushed"})

    with caplog.at_level(logging.WARNING):
        assert await manager.prune(idle=0.0) == []

    assert (workspace / "unsaved.md").exists()
    assert "does not have" in caplog.text


async def test_a_commit_the_mirror_has_not_seen_survives_the_sweep(
    manager: WorkspaceManager, owner: User
) -> None:
    # What a dirty-tree check would miss entirely: the agent committed its own
    # work, so the tree is clean and reads as untouched, while what it sits on is
    # a commit no push ever carried. Comparing snapshots is what sees it.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(
        await a_workspace(manager, thread, owner), mirror
    )
    await manager.save(thread, owner.id)
    await a_turn(workspace, {"work.md": "done"})
    await run_git("add", "-A", cwd=workspace)
    await run_git(*IDENTITY, "commit", "-m", "the agent's own", cwd=workspace)

    assert await run_git("status", "--porcelain", cwd=workspace) == ""
    assert await manager.prune(idle=0.0) == []

    assert workspace.exists()


async def test_a_folder_no_row_owns_is_reported_and_left(
    manager: WorkspaceManager, caplog: pytest.LogCaptureFixture
) -> None:
    # The row is written before the folder, so a folder without one is something
    # this manager did not make, and the sweep has no business deleting it.
    stray = manager.volumes_dir / str(uuid7()) / "tree" / str(uuid7())
    named = manager.volumes_dir / str(uuid7()) / "tree" / "not-a-volume"
    for path in (stray, named):
        path.mkdir(parents=True)

    with caplog.at_level(logging.WARNING):
        assert await manager.prune(idle=0.0) == []

    assert stray.is_dir()
    assert named.is_dir()
    assert caplog.text.count("has no volume row") == 2


async def test_a_fork_points_at_the_projects_upstream(
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    # What an agent means by pushing is the repository people will read it in, not
    # the mirror it happens to have been forked from — and not something that
    # differs by host, which is what inheriting the fork mechanism's answer gives.
    upstream = await a_mirror(tmp_path / "upstream", {"readme.md": "hello"})
    mirror = tmp_path / "mirror"
    await run_git("clone", str(upstream), str(mirror))

    workspace = await a_tree(manager, owner, mirror)

    url = await run_git("remote", "get-url", "origin", cwd=workspace)
    assert url.strip() == str(upstream)


async def test_a_fork_of_a_folder_project_has_no_remote(
    manager: WorkspaceManager, owner: User
) -> None:
    # A directory upstream is not a push target, so its fork says so by having
    # nowhere to push rather than by failing when someone tries.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})

    workspace = await a_tree(manager, owner, mirror)

    assert await run_git("remote", cwd=workspace) == ""


async def test_a_fork_arrives_installed_however_it_was_made(
    manager: WorkspaceManager, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exports carry no host installs, so every fresh fork installs for itself.
    probing(monkeypatch)
    mirror = await a_project_mirror(manager, {Probe.lockfile: "one"})
    await install(mirror)

    workspace = await a_tree(manager, owner, mirror)

    assert runs(workspace) == 1


async def test_a_thread_in_no_project_forks_an_empty_workspace(
    manager: WorkspaceManager, owner: User, tmp_path: Path
) -> None:
    # A tree like any other, through the same `materialize` — forked from the
    # blank mirror rather than a project's, and landing on the thread's own branch
    # exactly as a project fork does. A repository, not a directory: the run may
    # write in it, and what a run does with git works because HEAD resolves.
    thread_id = await a_thread(str(uuid7()))
    workspace = await manager.resolve(thread_id, None, owner.id)

    assert isinstance(workspace, ChatWorkspace)
    assert workspace.volume.project_id is None
    assert (
        workspace.path
        == (
            tmp_path / "volumes" / str(owner.id) / "tree" / str(workspace.volume.id)
        ).resolve()
    )
    # Known before anything is forked, which is what lets a runtime be told where it
    # will run before the process that runs there exists.
    assert not workspace.path.exists()

    async with manager.enter(workspace) as chat:
        assert (chat.path / ".git").is_dir()
        branch = await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=chat.path)
        assert branch == f"octomate/thread-{thread_id}\n"
        (chat.path / "scratch.txt").write_text("mine")


async def test_a_chat_workspace_is_not_the_threads_project_workspace(
    manager: WorkspaceManager, owner: User
) -> None:
    # A thread's chat tree and its project tree are separate volumes, so a chat
    # fork is never resumed into by the thread's first project turn — which would
    # then run in an empty repository instead of the project's code.
    thread = await a_bound_thread(manager)

    async with manager.open(thread.id, None, owner.id) as chat:
        chat_path = chat.path

    opened = await a_workspace(manager, thread, owner)
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    workspace = await manager.materialize(opened, mirror)

    assert workspace != chat_path
    assert (workspace / "readme.md").read_text() == "hello"


async def test_the_sweep_leaves_a_chat_workspace_alone(
    manager: WorkspaceManager, owner: User
) -> None:
    # It is its run's to throw away and is kept nowhere the sweep could restore it
    # from, so the pruner must not be the one to decide.
    thread_id = await a_thread(str(uuid7()))
    async with manager.open(thread_id, None, owner.id) as chat:
        assert await manager.prune(idle=0.0) == []
        assert chat.path.is_dir()


async def test_leaving_a_chat_workspace_discards_it(
    manager: WorkspaceManager, owner: User
) -> None:
    # Nothing survives the turn: the next run of the same conversation is a fresh
    # empty tree, at the same path and on the same row — which is what the
    # runtimes key a resumable session by.
    thread_id = await a_thread(str(uuid7()))
    workspace = await manager.resolve(thread_id, None, owner.id)

    async with manager.enter(workspace) as chat:
        (chat.path / "scratch.txt").write_text("mine")

    assert not workspace.path.exists()

    async with manager.open(thread_id, None, owner.id) as second:
        assert second.volume.id == workspace.volume.id
        assert second.path == workspace.path
        assert not (second.path / "scratch.txt").exists()


async def test_leaving_a_chat_workspace_a_later_run_is_in_keeps_it(
    manager: WorkspaceManager, owner: User
) -> None:
    # A mid-run follow-up starts its turn before the turn it supersedes has finished
    # losing its own. Both are in this tree, and the first to leave must not take
    # the floor out from under the second.
    thread_id = await a_thread(str(uuid7()))
    superseded = await manager.resolve(thread_id, None, owner.id)
    follow_up = await manager.resolve(thread_id, None, owner.id)

    async with manager.enter(superseded):
        await manager.prepare(follow_up)
        assert follow_up.path == superseded.path

    assert follow_up.path.is_dir()

    await manager.discard(follow_up)

    assert not follow_up.path.exists()


async def test_the_next_run_throws_away_what_a_killed_one_left(
    manager: WorkspaceManager, owner: User
) -> None:
    # Nothing ever resumes into a chat tree, so a fork that died half-made is
    # reclaimed rather than resumed into — which is why claiming one clears it, and
    # why it needs no staging directory to be forked through.
    workspace = await manager.resolve(await a_thread(str(uuid7())), None, owner.id)
    workspace.path.mkdir(parents=True)
    (workspace.path / "half.txt").write_text("from a run nobody finished")

    async with manager.enter(workspace) as chat:
        assert not (chat.path / "half.txt").exists()
        assert (chat.path / ".git").is_dir()


async def test_a_chat_run_leaves_nothing_in_the_blank_mirror(
    manager: WorkspaceManager, owner: User
) -> None:
    # The mirror is what makes a project tree disposable; this one has nothing to
    # be disposable about. A ref per conversation for work nobody intends to keep
    # is the opposite of what a mirror is for.
    thread = await a_chat_thread()

    async with manager.open(thread.id, None, owner.id) as chat:
        (chat.path / "scratch.txt").write_text("mine")
        await manager.save(thread, owner.id)

    blank = await manager.mirrors.create()
    refs = await run_git("for-each-ref", "--format=%(refname)", cwd=blank)
    # Its own empty commit and nothing else — no tree ref, no snapshot.
    assert refs == "refs/heads/main\n"


async def test_a_chat_workspace_goes_even_when_the_run_raised(
    manager: WorkspaceManager, owner: User
) -> None:
    # The files an interrupted run wrote are exactly the files nothing will ever ask
    # for again, and a failed turn on a thread nobody resumes would otherwise hold
    # its disk for good.
    workspace = await manager.resolve(await a_thread(str(uuid7())), None, owner.id)

    with suppress(RuntimeError):
        async with manager.enter(workspace) as chat:
            (chat.path / "scratch.txt").write_text("mine")
            raise RuntimeError("the provider dropped the connection")

    assert not workspace.path.exists()


async def test_an_unregistered_chat_runs_in_a_temporary_directory(
    manager: WorkspaceManager,
) -> None:
    # An unregistered sender has no root to hold a volume: an empty directory with
    # no git, the same path every turn so a runtime can resume, gone afterwards.
    thread_id = uuid7()
    workspace = await manager.resolve(thread_id, None, None)

    assert isinstance(workspace, UnregisteredWorkspace)
    assert (
        workspace.path
        == (
            Path(tempfile.gettempdir()) / "octomate-unregistered" / str(thread_id)
        ).resolve()
    )

    async with manager.enter(workspace) as chat:
        assert chat.path.is_dir()
        assert list(chat.path.iterdir()) == []

    assert not workspace.path.exists()
    async with async_session() as session:
        assert await session.list(TreeVolume, limit=None) == []


async def test_an_unregistered_sender_is_refused_a_project(
    manager: WorkspaceManager,
) -> None:
    project = manager.projects.get("inky")
    assert project is not None
    thread = await a_bound_thread(manager)

    with pytest.raises(UnregisteredProjectError):
        async with manager.open(thread.id, project, None):
            pytest.fail("an unregistered project workspace was opened")


async def test_leaving_a_project_workspace_keeps_it(
    manager: WorkspaceManager, owner: User
) -> None:
    # The other variant's whole ending: a project's tree outlives the run that
    # used it, which is what makes the next turn a resume.
    thread = await a_bound_thread(manager)
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    workspace = await a_workspace(manager, thread, owner)
    await manager.materialize(workspace, mirror)

    # Entering one that is already there is a resume, not a fork: the mirror is not
    # synced for it, and the tree is answered as the last turn left it.
    async with manager.enter(workspace):
        (workspace.path / "work.md").write_text("done")

    assert (workspace.path / "work.md").read_text() == "done"
    assert (workspace.path / "readme.md").read_text() == "hello"
    assert (
        await run_git("show", f"{tree_ref(workspace.volume.id)}:work.md", cwd=mirror)
        == "done"
    )


async def test_project_exit_checkpoints_a_failed_run(
    manager: WorkspaceManager, owner: User
) -> None:
    thread = await a_bound_thread(manager)
    workspace = await a_workspace(manager, thread, owner)
    (workspace.project.root / "readme.md").write_text("hello")
    await manager.prepare(workspace)
    (workspace.path / "work.md").write_text("unfinished")
    with pytest.raises(RuntimeError, match="provider failed"):
        async with manager.enter(workspace):
            raise RuntimeError("provider failed")
    assert (workspace.path / "work.md").read_text() == "unfinished"
    assert await manager.saved(workspace.volume)


async def test_project_exit_keeps_work_when_the_checkpoint_fails(
    manager: WorkspaceManager, owner: User, caplog: pytest.LogCaptureFixture
) -> None:
    thread = await a_bound_thread(manager)
    workspace = await a_workspace(manager, thread, owner)
    (workspace.project.root / "readme.md").write_text("hello")
    async with manager.enter(workspace):
        (workspace.path / "work.md").write_text("unfinished")
        shutil.rmtree(manager.mirrors.path(workspace.project))
    assert (workspace.path / "work.md").read_text() == "unfinished"
    assert not await manager.saved(workspace.volume)
    assert "could not be saved" in caplog.text


async def test_checkpoint_skips_unchanged_work_but_keeps_branch_changes(
    manager: WorkspaceManager, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = await a_bound_thread(manager)
    workspace = await a_workspace(manager, thread, owner)
    (workspace.project.root / "readme.md").write_text("hello")
    git = AsyncMock(wraps=snapshots.run_git)
    monkeypatch.setattr(snapshots, "run_git", git)
    async with manager.enter(workspace):
        (workspace.path / "work.md").write_text("unfinished")
    await manager.save(thread, owner.id)
    assert sum(call.args[0] == "fetch" for call in git.await_args_list) == 1

    await run_git("checkout", "-b", "renamed", cwd=workspace.path)
    await manager.save(thread, owner.id)
    assert sum(call.args[0] == "fetch" for call in git.await_args_list) == 2
    await manager.release(workspace.volume)
    await manager.prepare(workspace)
    assert await branch(workspace.path) == "renamed"
    assert (workspace.path / "work.md").read_text() == "unfinished"


@pytest.mark.parametrize("kind", ["project", "chat", "unregistered"])
async def test_context_cancellation_preserves_projects_and_releases_chat_claims(
    manager: WorkspaceManager, owner: User, kind: str
) -> None:
    thread = (
        await a_bound_thread(manager) if kind == "project" else await a_chat_thread()
    )
    project = await thread.project
    if project is not None:
        (project.root / "readme.md").write_text("hello")
    workspace = await manager.resolve(
        thread.id, project, None if kind == "unregistered" else owner.id
    )
    with anyio.CancelScope() as scope:
        async with manager.enter(workspace):
            (workspace.path / "work.md").write_text("unfinished")
            scope.cancel()
            await checkpoint()
    if kind == "project":
        assert isinstance(workspace, ProjectWorkspace)
        assert (workspace.path / "work.md").read_text() == "unfinished"
        assert not await manager.saved(workspace.volume)
    else:
        assert not workspace.path.exists()
    assert not manager.chatting


@pytest.mark.parametrize("overlapping", [False, True])
async def test_failed_chat_entry_releases_only_its_own_claim(
    manager: WorkspaceManager,
    owner: User,
    overlapping: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = await a_chat_thread()
    first = await manager.resolve(thread.id, None, owner.id)
    if overlapping:
        await manager.prepare(first)
    failed = await manager.resolve(thread.id, None, owner.id)
    monkeypatch.setattr(
        manager, "materialize", AsyncMock(side_effect=RuntimeError("prepare failed"))
    )
    with pytest.raises(RuntimeError, match="prepare failed"):
        async with manager.enter(failed):
            pytest.fail("a failed workspace was entered")
    if overlapping:
        assert first.path.is_dir()
        assert manager.chatting == {first.key: 1}
        await manager.discard(first)
    assert not manager.chatting
    assert not failed.path.exists()


async def test_a_resumed_workspace_can_be_pruned_again(
    manager: WorkspaceManager, owner: User
) -> None:
    # What the last push carried is recorded in the tree, and a resumed tree is
    # not the one that recorded it. Without the resume saying so, a thread that
    # was resumed and then went quiet would hold its disk until it next ran, on
    # the strength of work the mirror already has.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    await a_turn(await manager.materialize(opened, mirror), {"work.md": "done"})
    await manager.save(thread, owner.id)
    await manager.release(opened.volume)

    await manager.materialize(opened, mirror)

    assert await manager.saved(opened.volume)
    assert await manager.prune(idle=0.0) == [opened.volume.id]


async def test_a_fork_in_progress_is_not_a_workspace_to_prune(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner)
    workspace = await manager.materialize(opened, mirror)
    await manager.save(thread, owner.id)
    staging = workspace.with_name(f".{uuid7()}.forking")
    staging.mkdir()

    assert await manager.prune(idle=0.0) == [opened.volume.id]

    assert staging.exists()


async def test_the_sweep_prunes_on_its_interval(
    manager: WorkspaceManager, owner: User
) -> None:
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    thread = await a_bound_thread(manager)
    workspace = await manager.materialize(
        await a_workspace(manager, thread, owner), mirror
    )
    await manager.save(thread, owner.id)
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
    manager: WorkspaceManager, owner: User
) -> None:
    # The default branch is the wrong answer often enough — continuing
    # someone's feature branch, reproducing against a tag, working from a PR head.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    await run_git("checkout", "-b", "feat/theirs", cwd=mirror)
    (mirror / "theirs.md").write_text("wip")
    await run_git("add", "-A", cwd=mirror)
    await run_git(*IDENTITY, "commit", "-m", "theirs", cwd=mirror)
    await run_git("checkout", "main", cwd=mirror)
    thread = await a_bound_thread(manager)
    opened = await a_workspace(manager, thread, owner, "feat/theirs")

    workspace = await manager.materialize(opened, mirror)

    assert opened.volume.base_ref == "feat/theirs"
    assert (workspace / "theirs.md").read_text() == "wip"
    # Started there, but still on the thread's own branch: where the work begins
    # and whose branch it lands on are separate questions.
    assert await branch(workspace) == f"octomate/thread-{thread.id}"


async def test_a_ref_the_mirror_does_not_have_fails_the_fork(
    manager: WorkspaceManager, owner: User
) -> None:
    # A starting point that does not resolve has to fail where it was asked for.
    # Falling back to the default branch would be a thread quietly doing its work
    # somewhere nobody chose.
    mirror = await a_project_mirror(manager, {"readme.md": "hello"})
    opened = await a_workspace(
        manager, await a_bound_thread(manager), owner, "no/such/ref"
    )

    with pytest.raises(GitCommandError):
        await manager.materialize(opened, mirror)

    # And the half-made fork is gone, so the next turn forks again rather than
    # reading a staging directory as this tree.
    assert not opened.path.exists()
