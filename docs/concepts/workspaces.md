# Workspace design

[Workspaces](../usage/workspaces.md) says what a workspace does. This
page says why it is built the way it is. The code is
`octomate/managers/workspaces/`, three modules in the order the story runs:
`mirrors` keeps one pristine checkout per project, `dependencies` installs into a
tree without caring where it came from, and `base` forks a mirror into the
workspace a thread runs in and decides what becomes of it. Each knows only the
layer below.

## One mechanism for every project

**Every project has a mirror, and a mirror is always a git repository.** A project
with a remote upstream gets one by cloning. A project whose upstream is a plain
directory gets one by `git init` plus a commit of the folder's contents, made through
a scratch index with the folder as the work tree, so no `.git` is ever created inside
the folder itself. Syncing a directory upstream is the same job as fetching a
remote: copy in, `git add -A`, commit if anything changed.

That collapses the variants. There is no "repo project" and "documents project"
with separate forking and review stories; the only difference is whether the mirror
has an `origin`. A folder of documents gets branches, diffs and the same
save-and-restore lifecycle for free, and git is the merge.

The mirror is a normal working checkout, not a bare clone, kept on its default
branch. No run ever writes to it. That is what makes copying it hand a fork a
ready-to-run tree.

## Forking

A workspace is a **complete independent repository**: its own `.git`, its own object
store. A `git worktree` fails that requirement, because its `.git` points back at
the main repository, so every commit writes outside the workspace and Codex's
workspace sandbox refuses it.

The mechanism is probed once at startup by cloning a probe file inside the
workspaces directory:

| Host filesystem | Mechanism | Cost |
|---|---|---|
| APFS, btrfs, XFS | `cp -a -c` or `cp -a --reflink=always` | no bytes copied, a few milliseconds |
| ext4 and others | `git clone <mirror>` | objects hardlinked, tree copied |

The fork lands in a hidden staging directory beside its final path, and the last
step is a rename. A workspace directory that exists is therefore a workspace that is
complete. If anything fails, the staging directory is removed.

After the copy, the fork's `origin` is replaced with the mirror's own, which is the
project's real upstream, and every `refs/octomate/*` ref is deleted so a copied
fork does not carry other threads' snapshots in `git log --all`.

### Dependencies

A copied mirror should hand its fork a working environment, not a tree that needs
`uv sync` before the first command. Two things make that true.

The package managers already share. `uv`, `pnpm` and `npm` keep a global
content-addressed store and hardlink out of it, so an install in a fresh workspace
fetches nothing and costs almost no disk. Octomate leaves those stores at their
default paths on purpose.

A warm mirror survives copying only if its environment is relocatable.
`node_modules` copies cleanly. A Python `.venv` does not: an ordinary uv venv writes
the mirror's absolute interpreter path into every console script, so a copied
`.venv/bin/<tool>` keeps running the mirror's interpreter. Nothing errors; the
workspace simply is not the environment in use. So the install for a uv tree is
`uv venv --relocatable` then `uv sync`, and a copied venv reports the fork's own
prefix. The node managers need one command, and it is `npm install` rather than the
`npm ci` their documentation recommends, because `ci` deletes the `node_modules`
the warm mirror exists to keep.

The tree records the hash of the lockfile it was last installed from in
`.git/octomate-installed`. A copied fork brings the stamp along and does nothing; a
cloned fork has a fresh `.git`, no stamp, and installs from the store the mirror
warmed. An install that fails is logged and the tree handed over uninstalled: a host
without `pnpm` should lose its dependencies, not its workspace.

## Saving

A workspace is a cache. That is what makes reclaiming it a disk decision rather
than a data-loss decision.

After every turn the whole tree is snapshotted: the index is copied to a scratch
file, `git add -A` runs through it, and `commit-tree` builds a commit whose parent is
HEAD. HEAD does not move and no branch points at the commit, exactly as no branch
points at a stash. The branch therefore keeps only the agent's own commits, a
workspace in the middle of something is not finished on its behalf, and no machine
`wip` commit ends up in the history a person reviews. `git stash create` is the
obvious tool and the wrong one: it captures tracked changes only, and an agent's
first turn is mostly new files.

The snapshot is pushed to the mirror's local path under
`refs/octomate/threads/<thread_id>`. A ref namespace rather than `refs/heads/` keeps
`git branch` clean and keeps the refs out of ordinary clones. The workspace records
the same commit under `refs/octomate/saved`, which is what the sweep and `dispel`
consult: a `git status` cannot tell whether the mirror has seen the tree, because
nothing here commits, so every saved workspace is also dirty.

Restoring puts the tree back the way the turn left it: the branch to the snapshot's
parent, the tree from the snapshot, then the index reset so uncommitted work reads
as uncommitted. Modifications, deletions, untracked files and a detached HEAD make
the round trip. The staged/unstaged split does not, since one tree cannot hold both,
and neither do ignored files. Reconciling history that comes back is the agent's
job.

## Reclaiming

The sweep runs on a timer and releases workspaces idle past the window, skipping
anything the mirror has not seen. Being wrong costs a slow resume, never lost work,
so the heuristic does not need to be clever. The chat directory is never visited:
the sweep only looks at entries named by a thread id.

`dispel` is the same release on the agent's word instead of the timer. The release
waits for the turn to end rather than happening in the call, because a run's
working directory is fixed when its process spawns and nothing is pulled out from
under a run still in it.

## Binding

Materialization is runtime machinery, not something an agent decides: given a
thread and a project there is exactly one correct workspace. Two of its inputs are
genuine judgment calls that belong to whoever is asking: which project the thread is
about, and which ref to start from. The default branch is the wrong answer often
enough, for continuing a feature branch, reproducing against a tag, or working
from a pull request head.

So binding is the gateway's `teleport` with a `project`, and Octomate keeps
everything that is policy:

| The agent supplies | Octomate decides |
|---|---|
| project | the path |
| ref, optionally | the fork mechanism |
| | whether this user may bind this project |
| | serialization against a concurrent fork |
| | the branch, and its lifecycle |

A thread binds once. Re-binding is refused rather than switching, which removes the
case where an agent calls twice and silently destroys the first workspace, and keeps
a thread's history honest: what a thread is about does not change underneath the
record of what it did.

Binding takes effect by ending the turn. A run's working directory cannot change
mid-process, and a model told to wait would go on working in the tree it started
in. So the run ends on the teleport, the graph forks the workspace, and the same
conversation resumes inside it. The `ref` is validated against the mirror before
the move, because a thread that bound and then could not check out would be stuck.

## The chat workspace

A process always has a working directory, so a thread with no project still needs
one. It used to fall back to the agent's configured directory, which on a server
was Octomate's own install directory, beside the database and the provider keys.

Instead it gets a fork of a prepared empty repository at `mirrors/.blank`, made by
the same code path as every other fork, and thrown away when the run ends. What
binding changes is only the ending: a project thread's tree is saved and resumed,
a chat thread's is discarded. That is a better thing to tell a model than "you may
not write": binding is what makes your work kept.

The path is the thread's even though the tree is the run's. Both Claude and Codex
key a resumable session by the directory it ran in, and Claude's `--resume` from
anywhere else answers "no conversation found", so a per-run directory would cost
every chat thread its memory. The fork lands at `workspaces/chat/<thread_id>` every
time, emptied and re-forked. Two overlapping runs of one conversation share the
tree, counted, so the first to leave does not delete the floor from under the
second.

## Mirror sync

Materialization fetches unless the mirror was synced within `freshness_window`,
which starts at zero so that every first fork fetches. A failed fetch degrades to
the stale mirror with a warning; a mirror that cannot be created fails the fork,
because there is nothing to fall back to. Concurrent forks of one mirror serialize
on a per-mirror lock. A resumed workspace never syncs at all, which is what keeps
continuing a thread independent of the upstream being reachable.

The window is purely a performance knob. It stays at zero until the cost of a fetch
has been measured where the server actually runs.

## Still open

- The freshness window's real value.
- Large binaries in a directory-mirrored folder: git keeps a full copy per version
  of what it cannot delta-compress.
- How a reviewed change returns to a directory-upstream folder. The copy-back is
  deliberate by design, but who performs it, and what it looks like in a channel,
  is unspecified.
