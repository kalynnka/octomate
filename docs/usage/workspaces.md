# Workspaces

A driven run needs a directory to work in. Octomate gives every thread its own
**workspace**: a git checkout forked from a pristine **mirror** of a declared
[project](projects.md). A thread bound to a project keeps its workspace between turns
and saves its work to the mirror after every turn. A thread in no project gets an
empty, disposable one.

## Where things live

Everything is under `.octomate/` in the server's working directory:

```
.octomate/
  mirrors/<project>/            pristine checkout on its default branch; no run writes here
  mirrors/.blank/               an empty repository, the chat workspace's upstream
  workspaces/<thread_id>/       one per project-bound thread, kept between turns
  workspaces/chat/<thread_id>/  one per run of a thread in no project, discarded after
```

The workspace branch is `octomate/thread-<thread_id>`. The fork's `origin` is the
project's real upstream, so `git push -u origin <branch>` from inside a run reaches
the repository people read. A directory-upstream project's fork has no remote.

## What happens each turn

- **First turn** forks the mirror into the workspace and checks out the thread's
  branch, from the mirror's default branch or from the requested `ref`.
  Dependencies are installed if the tree has a `uv.lock`, `pnpm-lock.yaml` or
  `package-lock.json`.
- **Every turn** ends by snapshotting the whole tree, uncommitted and untracked files
  included, into the mirror under `refs/octomate/threads/<thread_id>`. The branch
  itself is untouched, so the history a reviewer reads holds only the agent's own
  commits.
- **Later turns** reuse the workspace as it stands, without syncing the mirror
  again. Continuing a thread never waits on the upstream being reachable.
- **Idle workspaces** are reclaimed by a sweep, by default after 24 hours unused.
  A tree is only released once the mirror holds its latest snapshot. The next
  message on that thread forks it again and restores the snapshot: files,
  deletions, untracked work and a detached HEAD all come back. Ignored files such
  as a `.venv` are rebuilt, not restored.
- **`dispel`** is the agent giving the workspace back early, once the work is
  merged, delivered or dropped. Same rule: saved first, released only if the save
  took.

## The chat workspace

A thread in no project still runs somewhere. It gets a fork of an empty repository
under `workspaces/chat/`, on the same kind of branch, writable within the agent's
permission mode, and thrown away when the run ends. Nothing is saved.

So "write me a script" followed by "now run it" fails in a chat thread: the second
turn starts in a fresh empty tree. The conversation persists, the filesystem does
not. Binding the thread to a project is what makes work kept. Inkling is the one
exception: with no project it gets no workspace and no file or shell tools at all.

## Per agent

| Agent | How the workspace is used |
|---|---|
| Claude Code | The SDK's `cwd`. The project's `extra_roots` are passed as additional directories. Claude keys resumable sessions by directory, so binding moves the session transcript along with the run. |
| Codex | The thread's `cwd`, and the write boundary of its sandbox in `user_review` and `auto_review` modes. `full_access` removes the sandbox. |
| DeepSeek Harness | The session's `cwd`, fixed when the dsh session is created. The harness mounts no gateway, so it cannot bind a project from inside a turn: bind from Trunkline first. |
| Inkling | The root of its file, shell and repository tools, all behind approval. No project, no tools. |

What a run may do inside the workspace is the runtime's own permission mode, not
something Octomate adds. See [Permissions and approvals](agents/permissions.md).

## Settings

```yaml
mirrors:
  freshness_window: 0      # seconds a synced mirror is fresh enough to fork again
  identity:                # the identity on commits Octomate itself makes
    name: octomate
    email: octomate@example.com
workspaces:
  idle_window: 86400       # seconds unused before a workspace may be reclaimed
  sweep_interval: 3600     # seconds between sweeps
```

The mirror and workspace directories are not configurable; they follow the working
directory the server starts in. The [design page](../concepts/workspaces.md)
explains the fork mechanism, snapshots and the sweep in detail.
