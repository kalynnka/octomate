from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self, overload

from octomate.config.mirrors import GitIdentity
from octomate.config.workspaces import WorkspacesConfig
from octomate.managers.mirrors import GitCommandError, MirrorManager, run_git
from octomate.managers.project import ProjectManager
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread

logger = logging.getLogger(__name__)

# `cp` forks a file by cloning its extents instead of copying its bytes, with `-c`
# on BSD and `--reflink=always` on GNU. Each flag is an error on the other's `cp`,
# and neither says anything about the filesystem underneath, so which one works is
# probed rather than dispatched on by platform.
REFLINK_FLAGS = ("-c", "--reflink=always")


# The snapshot the last push carried, kept in the workspace itself. A workspace
# that no longer snapshots to the same thing holds work the mirror has never
# seen, which is the one thing the pruner may not throw away.
SAVED_REF = "refs/octomate/saved"

# What a snapshot's message carries, so a resume knows which branch to put the
# workspace back on. A detached HEAD is called `HEAD`, git's own name for it,
# which is also how it goes back.
SNAPSHOT_PREFIX = "octomate: "


def thread_ref(thread_id: uuid.UUID) -> str:
    """Where a thread's work is kept in its project's mirror.

    Outside `refs/heads/`, so the mirror does not grow a branch per thread, an
    ordinary clone of it carries none of them, and `git branch` in a fresh
    workspace shows the project's branches rather than everyone's threads.
    """
    return f"refs/octomate/threads/{thread_id}"


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


@dataclass
class Workspace(ABC):
    """The tree one run happens in, and what becomes of it when the run ends.

    Handed out by `WorkspaceManager.open` and entered by the run that was given it.
    Knowing where it is costs nothing — the path is the thread's — so a caller has
    it before anything is forked, which is what lets a runtime be configured with
    its own working directory before the process that will run there exists.

    Two variants, and they differ in exactly three things: where the tree lives,
    which mirror it is forked from, and whether leaving it keeps or throws away
    what the run did. Everything else — the fork itself, the branch it lands on,
    the lock that stops two turns forking it twice — is one mechanism on the
    manager, and both go through it.
    """

    workspaces: WorkspaceManager
    thread_id: uuid.UUID

    @property
    @abstractmethod
    def path(self) -> Path:
        """Where this run happens. Absolute, because it becomes the working
        directory of a process Octomate did not start in its own, and known
        without touching the disk."""

    @abstractmethod
    async def prepare(self) -> Path:
        """The tree, forked and ready for a run to happen in."""

    async def __aenter__(self) -> Self:
        await self.prepare()
        return self

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """End the run's claim on the tree, however the run ended."""


@dataclass
class ProjectWorkspace(Workspace):
    """This thread's own checkout of its project, at ``workspaces_dir/<thread_id>``.

    A thread runs here rather than in `project.root`, so two threads on one project
    cannot walk over each other's uncommitted work — and the root stays the
    person's, untouched by anything an agent does.
    """

    project: Project

    @property
    def path(self) -> Path:
        return self.workspaces.path(self.thread_id)

    async def prepare(self, ref: str | None = None) -> Path:
        """This thread's checkout, resumed as it stands or forked when it is gone.

        Only a thread's first turn does any of that. A workspace that exists is
        resumed into as it stands, and its mirror is not synced for it either: a
        workspace is not re-made from the mirror once it is there, so a fetch would
        buy the turn nothing and would couple continuing a thread to the upstream
        being reachable. `materialize` answers an existing workspace anyway; the
        check is here to keep the sync in front of it from running — and to stamp
        the workspace, which is what tells the sweep this thread is still running.
        """
        workspace = self.workspaces.existing(self.thread_id)
        if workspace is not None:
            return workspace
        mirror = await self.workspaces.mirrors.sync(self.project)
        return await self.workspaces.materialize(self, mirror, ref)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Nothing. A project's workspace outlives the run that used it — that is
        what makes the next turn a resume — and what the turn owes is a `save`,
        which the graph does once for the turn rather than once per agent that
        ran in it."""


@dataclass
class ChatWorkspace(Workspace):
    """The tree a thread in no project runs in: a fork of the empty repository, at
    ``workspaces_dir/chat/<thread_id>``, gone when the run ends.

    A workspace rather than a bare directory, so a run in no project is a run like
    any other — it may write, unpack, and script inside its own tree, bounded by
    the same directory every run is bounded by. Forking nothing is what makes that
    affordable: an empty repository costs a `git init`'s worth of disk, and binding
    to a project stops meaning "you may write now" and starts meaning "your work is
    kept now".

    Under `chat/` rather than among the project workspaces, because up there the
    directory's existence is the answer to "is this thread's workspace already
    forked": a chat fork left at `path(thread_id)` would be resumed into by the same
    thread's first project turn, which would then run in an empty repository instead
    of the project's code.

    The path is the thread's and does not change between turns, even though what is
    in it does. Both runtimes key a resumable session by the directory it ran in —
    Claude's `--resume` answers "no conversation found" from anywhere else — so a
    tree named after the run rather than the thread would be a run that cannot
    continue the conversation it belongs to.
    """

    @property
    def path(self) -> Path:
        return (self.workspaces.workspaces_dir / "chat" / str(self.thread_id)).resolve()

    async def prepare(self) -> Path:
        """An empty tree of this run's own, forked from the blank mirror.

        Claimed first, which is what throws away whatever a killed run left at this
        path: nothing resumes a chat workspace, so anything there when nobody is in
        it belongs to a run that is over.

        Nothing here is ever saved. The blank mirror would otherwise grow a ref per
        conversation for work nobody intends to keep, which is the opposite of what
        a mirror is for.
        """
        await self.workspaces.claim(self)
        # The mirror of no project, which is what this thread is in.
        mirror = await self.workspaces.mirrors.create()
        return await self.workspaces.materialize(self, mirror)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Throw the tree away. In `__aexit__` rather than after the run's last
        event, so a turn that raised does not leave its files behind — they are
        exactly the files nothing will ever ask for again."""
        await self.workspaces.discard(self)


class WorkspaceManager:
    """Every project-bound thread's workspace: a fork of the project's mirror at
    ``workspaces_dir/<thread_id>``, checked out on the thread's own branch and
    released when the disk is wanted back.

    A fork is a complete, independent repository — its own `.git`, its own object
    store — made by cloning the mirror's extents where the filesystem does
    copy-on-write and by `git clone` where it does not. Never a `git worktree`,
    whose `.git` is a file pointing back at the mirror: commits in one write
    outside the workspace, which an agent's write sandbox refuses.

    A workspace is a cache rather than the only copy: each turn's tree is
    snapshotted and pushed to the mirror as the thread's own ref, so releasing one
    is a disk decision and resuming lays that ref back over a fresh fork. What the
    agent did with git in there is the agent's — nothing here commits.

    A thread in no project gets one too, at ``workspaces_dir/chat/<thread_id>``,
    forked from the blank mirror and thrown away when the run that made it ends.
    Same mechanism, opposite ending: there is no project to keep it for, so binding
    to one is what turns a run's tree into work that outlives the run.

    The whole lifecycle is here. A turn asks `open` for the workspace its thread
    runs in and enters it, which is what forks the tree and what ends it; the turn
    itself owes one more thing, `save`, which the graph does after the run. Those
    need the registry to say which project a thread is in and the mirrors to say
    where that project's is, which is why this holds both — which fork a thread gets
    is exactly what the registry says about it, so knowing what binds one is not a
    dependency it is reaching for. The layer below stays free of it: every method
    that touches git takes a mirror path and answers for a directory.
    """

    def __init__(
        self,
        *,
        projects: ProjectManager | None = None,
        mirrors: MirrorManager | None = None,
        config: WorkspacesConfig | None = None,
        workspaces_dir: Path = Path(".octomate/workspaces"),
    ) -> None:
        # A workspace is a project-bound thread's, so the registry that says which
        # project and the mirrors that say where are what this works through. Its
        # host replaces both with its own when it takes ownership — a second
        # registry would resolve no thread to a project, and a second mirror
        # manager would keep its own sync locks — so a manager built without them
        # is one nobody has handed to a host yet.
        self.projects = projects if projects is not None else ProjectManager()
        self.mirrors = mirrors if mirrors is not None else MirrorManager()
        self.config = config if config is not None else WorkspacesConfig()
        # Resolved once, here: a workspace path leaves this manager to become a
        # subprocess's cwd and a `GIT_INDEX_FILE` beside it, and the default is
        # relative to wherever Octomate was started. Pinning it at construction
        # also stops the root moving if anything ever chdirs underneath.
        self.workspaces_dir = workspaces_dir.resolve()
        self.reflink: str | None = None
        self.probed = False
        self.locks: dict[uuid.UUID, asyncio.Lock] = {}
        # How many runs are in each thread's chat workspace right now. The tree
        # is the run's, and two overlapping runs of one conversation share it —
        # this is what stops the first to finish taking it from the second.
        self.chatting: dict[uuid.UUID, int] = {}

    @property
    def identity(self) -> GitIdentity:
        """Who the machine is when it writes a snapshot — the `mirrors:` block's,
        read rather than copied, so a turn's work and a folder sync into a mirror
        carry one name and cannot drift into two."""
        return self.mirrors.config.identity

    def path(self, thread_id: uuid.UUID) -> Path:
        """Where this thread's workspace lives. Absolute, because it becomes the
        working directory of a process Octomate did not start in its own."""
        return (self.workspaces_dir / str(thread_id)).resolve()

    def existing(self, thread_id: uuid.UUID) -> Path | None:
        """This thread's workspace if it is already forked, else None — what saves a
        resumed turn the fork and the sync in front of it.

        Finding one stamps it as used, so the sweep measures idleness from the turn
        that just asked rather than from the last one that wrote something.
        """
        path = self.path(thread_id)
        if not path.is_dir():
            return None
        os.utime(path)
        return path

    @overload
    def open(self, thread_id: uuid.UUID, project: Project) -> ProjectWorkspace: ...

    @overload
    def open(self, thread_id: uuid.UUID, project: None) -> ChatWorkspace: ...

    @overload
    def open(self, thread_id: uuid.UUID, project: Project | None) -> Workspace: ...

    def open(self, thread_id: uuid.UUID, project: Project | None) -> Workspace:
        """The workspace a run in this thread happens in, ready to be entered.

        Which one it is, is the whole of what a project decides about a run: a
        thread in one gets a checkout of it that outlives the turn, and a thread in
        none gets an empty tree that does not. Nothing else about the two runs
        differs — both may write where they are, and both are bounded by it.

        `project` is passed rather than looked up because the caller has already
        asked, and the answer decides more than the directory. No disk is touched
        here: the path is settled, so a runtime can be told where it will run before
        anything is forked, and the fork happens when the run enters it.

        A thread in no project used to run in the agent's configured `cwd`, which
        defaults to `"."`: on a server, Octomate's own install directory.
        """
        if project is None:
            return ChatWorkspace(self, thread_id)
        return ProjectWorkspace(self, thread_id, project)

    async def claim(self, workspace: ChatWorkspace) -> None:
        """Count this run into `workspace`, and clear whatever the last one left.

        Counted, because a mid-run follow-up starts its turn before the turn it
        supersedes has finished losing its own: both runs are in this tree, and the
        first to leave must not take the floor out from under the second. They share
        it while they overlap, which is what two runs of one conversation are
        entitled to.

        A tree nobody is in belongs to a run that is over — one that was killed
        before it could discard — so it goes, and this run forks its own. That is
        also why a chat workspace needs no staging directory to be forked through:
        nothing ever resumes into one, so a fork that died half-made is thrown away
        here rather than mistaken for a tree.
        """
        async with self.locks.setdefault(workspace.thread_id, asyncio.Lock()):
            holders = self.chatting.get(workspace.thread_id, 0)
            if holders == 0 and workspace.path.exists():
                await asyncio.to_thread(shutil.rmtree, workspace.path)
            self.chatting[workspace.thread_id] = holders + 1

    async def discard(self, workspace: ChatWorkspace) -> None:
        """Give this tree back to the disk, once no run is still in it."""
        async with self.locks.setdefault(workspace.thread_id, asyncio.Lock()):
            holders = self.chatting.get(workspace.thread_id, 0) - 1
            if holders > 0:
                self.chatting[workspace.thread_id] = holders
                return
            self.chatting.pop(workspace.thread_id, None)
            if workspace.path.is_dir():
                await asyncio.to_thread(shutil.rmtree, workspace.path)

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

    async def materialize(
        self,
        workspace: Workspace,
        mirror: Path,
        ref: str | None = None,
    ) -> Path:
        """Fork `mirror` into `workspace`, and answer the tree.

        The one place a workspace is made, whichever kind it is: a project's
        checkout and a chat thread's empty tree are the same fork of the same
        shape, differing only in what they were forked from. Both land on the
        thread's own branch, and neither carries the mirror's `origin` unless the
        mirror has a real one to inherit.

        A workspace that already exists is answered as it stands — a project
        thread's live tree, uncommitted work included, where forking again would
        throw that work away, and for a chat thread the tree an overlapping run of
        the same conversation is already in. Serialized per thread, so two turns
        arriving together materialize once.

        The fork is made beside the workspace and moved into place, so a workspace
        only ever exists whole: a rename within one directory is atomic, where a
        copy the machine dies in the middle of would leave half a tree that every
        later turn reads as this thread's own. That is what makes "the directory is
        there" mean "the workspace is there", here and for a caller that asks the
        same question. A fork that merely fails is removed the same way, before the
        error travels on.
        """
        path = workspace.path
        async with self.locks.setdefault(workspace.thread_id, asyncio.Lock()):
            if path.is_dir():
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            flag = await self.detect()
            # Hidden, and named after the thread, so it is never mistaken for a
            # workspace and the next fork of this thread reclaims whatever a
            # killed one left — the case the cleanup below cannot reach.
            staging = path.with_name(f".{workspace.thread_id}.forking")
            if staging.exists():
                shutil.rmtree(staging)
            try:
                if flag is not None:
                    await copy(mirror, staging, flag)
                else:
                    await run_git("clone", str(mirror), str(staging))
                await self.inherit_remotes(mirror, staging)
                await self.checkout(workspace.thread_id, mirror, staging, ref)
                staging.rename(path)
                # `cp -a` preserves the mirror's timestamps and a rename does not
                # refresh them, so without this a fork inherits an mtime that can
                # already be past the idle window — a workspace made seconds ago,
                # born ready for the sweep.
                os.utime(path)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return path

    async def inherit_remotes(self, mirror: Path, workspace: Path) -> None:
        """Give the fork the mirror's own `origin`, or no remote where the mirror
        has none.

        Otherwise where a workspace points depends on how it was forked: a copy
        inherits the mirror's remotes, and a clone gets an `origin` pointing at the
        mirror itself. An agent asked to push its branch would find a different
        answer on a copy-on-write host than on one that clones, which is not
        something the host's filesystem gets to decide.

        The mirror's `origin` is the project's real upstream, and is what an agent
        means by pushing — a branch it named for the job, on the repository people
        will read it in. Nothing Octomate does travels that way: a turn is saved by
        pushing to the mirror's path, never through a remote name.

        A folder project's mirror has no upstream to inherit, and neither does its
        fork. There is nowhere to push a directory, and saying so by having no
        remote is clearer to an agent than an `origin` that fails when it is used.
        """
        if "origin" in (await run_git("remote", cwd=workspace)).split():
            await run_git("remote", "remove", "origin", cwd=workspace)
        if "origin" not in (await run_git("remote", cwd=mirror)).split():
            return
        url = await run_git("remote", "get-url", "origin", cwd=mirror)
        await run_git("remote", "add", "origin", url.strip(), cwd=workspace)

    async def checkout(
        self,
        thread_id: uuid.UUID,
        mirror: Path,
        workspace: Path,
        ref: str | None = None,
    ) -> None:
        """Put a freshly forked `workspace` back the way this thread's last turn left
        it, when `mirror` is keeping a snapshot of it.

        A thread whose workspace was pruned resumes here, and resumes into the
        state it was interrupted in rather than a tidied version of it: the branch
        goes back to where the agent's HEAD was, which is the snapshot's parent,
        and the tree it could see is laid down over that from the snapshot itself.
        The index then drops back to HEAD, so work the agent had not committed
        reads as uncommitted rather than as staged.

        A thread forking for the first time starts on its own branch, where the
        mirror's branch is — or where `ref` names, when the bind asked for one.
        The default branch is the wrong answer often enough: continuing someone's
        feature branch, reproducing against a tag, working from a PR head.

        `ref` is only consulted by a first fork. A thread whose workspace was
        pruned resumes into its own snapshot, which is where its work is, and no
        starting point competes with that.
        """
        saved = thread_ref(thread_id)
        if not await run_git("ls-remote", str(mirror), saved):
            branch = f"octomate/thread-{thread_id}"
            if ref is None:
                await run_git("checkout", "-b", branch, cwd=workspace)
                return
            # Fetched from the mirror rather than named directly: a clone's
            # remote-tracking refs are removed with the `origin` `inherit_remotes`
            # replaces, so the only commit a fork is certain to resolve is the one
            # it was left on. A ref the mirror does not have fails here, which is
            # what the caller has to be able to report.
            await run_git("fetch", str(mirror), ref, cwd=workspace)
            await run_git("checkout", "-b", branch, "FETCH_HEAD", cwd=workspace)
            return
        # A copied fork carries the mirror's refs already and a cloned one does not,
        # so both fetch: one to be sure it is current, the other to have it at all.
        await run_git("fetch", str(mirror), f"+{saved}:{saved}", cwd=workspace)
        subject = await run_git("show", "-s", "--format=%s", saved, cwd=workspace)
        branch = subject.strip().removeprefix(SNAPSHOT_PREFIX)
        head = (
            ("--detach", f"{saved}^")
            if branch == "HEAD"
            else ("-B", branch, f"{saved}^")
        )
        await run_git("checkout", *head, cwd=workspace)
        await run_git("read-tree", "-u", "--reset", saved, cwd=workspace)
        await run_git("reset", cwd=workspace)
        # A restored workspace is the mirror's snapshot by construction, and says
        # so: the ref that records it was local to the workspace this one replaces,
        # and without this a resume that never reached its next save would look
        # like unsaved work forever, which is a workspace that cannot be reclaimed.
        await run_git("update-ref", SAVED_REF, saved, cwd=workspace)

    async def snapshot(self, path: Path) -> str:
        """A commit holding this workspace exactly as it stands — staged, unstaged
        and untracked alike — made without disturbing it.

        Built rather than committed, so restoring a workspace is all this costs
        the repository inside it: HEAD stays where the agent left it, its branch
        carries the agent's commits and no others, and whatever the agent was in
        the middle of is not finished on its behalf. Resolving that history is the
        agent's job; putting the tree back is this one.

        The parent is HEAD, so the branch's own history comes along, and the
        snapshot belongs to no branch — reachable only from the thread's ref, the
        way a stash is reachable only from `refs/stash`. Its message carries the
        branch, which is the one thing a tree and a parent cannot say.

        The index it is written through is a copy of the workspace's, which keeps
        the real one untouched and carries git's stat cache with it, so staging
        the tree compares timestamps instead of rehashing every file.
        """
        index = path / ".git" / "octomate-snapshot-index"
        await asyncio.to_thread(shutil.copyfile, path / ".git" / "index", index)
        env = {"GIT_INDEX_FILE": str(index)}
        await run_git("add", "-A", cwd=path, env=env)
        tree = (await run_git("write-tree", cwd=path, env=env)).strip()
        branch = (await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)).strip()
        commit = await run_git(
            *self.identity.commit_flags,
            "commit-tree",
            tree,
            "-p",
            "HEAD",
            "-m",
            f"{SNAPSHOT_PREFIX}{branch}",
            cwd=path,
        )
        return commit.strip()

    async def save(self, thread: Thread) -> None:
        """Leave this thread's finished turn where losing its workspace cannot cost
        it: a snapshot of the whole tree, pushed to the project's mirror as the
        thread's own ref.

        Snapshot *and* push. A fork owns its objects outright — nothing in a
        workspace survives its removal — so without the push the workspace is the
        only copy of what the turn did, and reclaiming it would be losing work
        rather than evicting a cache. That is the whole of what makes a workspace
        disposable, and why the sweep is a disk decision.

        A thread with no workspace — in no project, or one whose turn never forked
        one — has nothing to leave. Nothing the agent asks for or is told about
        either: it knows the directory it was given, not that the directory is a
        fork, and not that the sweep will take it back.

        Forced, because the ref records what the workspace holds rather than a
        history to protect: an amend or a reset inside the workspace moves the ref
        with it. The mirror is the only place this ever pushes; nothing here
        reaches the upstream, which sees a thread's work when a person asks for a
        pull request and not before.

        A failure is logged rather than raised. The turn has already happened and
        already answered; failing it now would report a delivered answer as an
        error, and the next turn saves the same work again.
        """
        project = await self.projects.of(thread)
        path = self.path(thread.id)
        if project is None or not path.is_dir():
            return
        try:
            async with self.locks.setdefault(thread.id, asyncio.Lock()):
                snapshot = await self.snapshot(path)
                await run_git(
                    "push",
                    "--force",
                    str(self.mirrors.path(project)),
                    f"{snapshot}:{thread_ref(thread.id)}",
                    cwd=path,
                )
                # Only now, and only here: this is what tells the sweep that letting
                # this workspace go costs a resume rather than the work.
                await run_git("update-ref", SAVED_REF, snapshot, cwd=path)
        except (GitCommandError, OSError):
            logger.exception(
                "the workspace for thread %s could not be saved; its work is only "
                "in %s until a later turn saves it",
                thread.id,
                path,
            )

    async def saved(self, path: Path) -> bool:
        """Whether the mirror already holds everything in this workspace: the same
        tree, on the same commit, as the last push carried.

        The question the sweep has to answer before reclaiming anything, and one
        `git status` cannot answer any more — nothing here commits, so a saved
        workspace is a dirty workspace and being dirty says nothing. Snapshotting
        again and comparing does say it. A push that failed leaves a workspace
        that looks saved from the outside, which is why what it is compared
        against is a ref the push itself writes.
        """
        pushed = await run_git(
            "for-each-ref", "--format=%(objectname)", SAVED_REF, cwd=path
        )
        if not pushed.strip():
            return False
        state = ("show", "-s", "--format=%T %P")
        return await run_git(*state, pushed.strip(), cwd=path) == await run_git(
            *state, await self.snapshot(path), cwd=path
        )

    async def prune(self, idle: float) -> list[uuid.UUID]:
        """Release every workspace nothing has used for `idle` seconds, and answer
        with the threads that lost theirs.

        Eviction, not a decision about whether a thread is over: abandonment is
        unobservable — a thread parked on a question and one silently dropped look
        identical — so rather than guessing, being wrong here costs the next turn a
        fork instead of costing anyone their work.

        Which is only true of a workspace the mirror already has, so one holding
        anything unsaved is kept and said out loud. That covers a turn still
        running, too: it has changed something since its last turn was saved,
        almost by definition.
        """
        released: list[uuid.UUID] = []
        if not self.workspaces_dir.is_dir():
            return released
        for path in sorted(self.workspaces_dir.iterdir()):
            try:
                thread_id = uuid.UUID(path.name)
            except ValueError:
                # A staging directory, a probe, or `chat/`: none of them is a
                # workspace this may reclaim. A chat workspace is its run's to
                # throw away, and is not kept anywhere the sweep could restore it
                # from.
                continue
            if time.time() - path.stat().st_mtime < idle:
                continue
            if not await self.saved(path):
                logger.warning(
                    "the workspace for thread %s is idle but holds work the mirror "
                    "does not have; keeping it",
                    thread_id,
                )
                continue
            await self.release(thread_id)
            released.append(thread_id)
        if released:
            logger.info("released %d idle workspace(s)", len(released))
        return released

    async def sweep(self) -> None:
        """Prune on the interval, for as long as the host runs.

        A sweep that fails is logged and the next one still happens: reclaiming
        disk is maintenance, and a host that stops serving because it could not
        tidy up has the priority backwards.
        """
        while True:
            await asyncio.sleep(self.config.sweep_interval)
            try:
                await self.prune(self.config.idle_window)
            except Exception:
                logger.exception("the workspace sweep failed")

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
