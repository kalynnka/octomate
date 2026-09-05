# Thread Workspaces

Records what the workspace discussion settled and what it did not. The mirror,
the fork, running a project thread in it, and the lifecycle below — per-turn
save, pruning, resume — are built, as are teleporting into a project, the chat
workspace and dependency reuse.

A run currently happens in `project.root` — one directory on the Octomate host,
shared by every thread that resolves to that project. This document replaces that
with a workspace per thread, and says where one comes from, who may have one, and
when it goes away.

The code is `octomate/managers/workspaces/`, three modules in the order the story
runs: `mirrors` keeps one pristine checkout per project, `dependencies` installs
into a tree without caring where the tree came from, and `base` forks a mirror
into the workspace a thread runs in and decides what becomes of it. Each knows
only the layer below it.

## Layout

Everything lives under `.octomate/`, which is already gitignored.

```
.octomate/
  mirrors/<project>/        pristine checkout; no run ever writes here
  workspaces/<thread_id>/   one per project-bound thread
  mirrors/.blank/           an empty repository, the chat fork's upstream
  workspaces/chat/<thread_id>/  one per run of a thread in no project
```

**Every project has a mirror, and a mirror is always a git repository.** A project
with an upstream gets one by cloning it. A project without — a folder of
documents, a working area that was never a repository — gets one by `git init` at
registration time.

That is what collapses the variants. There is no "repo project" and "non-repo
project" with separate forking, review, and release stories; there is one
mechanism, and the only difference is whether the mirror has an `origin` to sync
with. A documents project gets branches, diffs, history, and the same
save-and-prune lifecycle for free, and the earlier objection — that forking
something with no merge operation manufactures divergence you cannot reconcile —
stops applying, because git *is* the merge.

The mirror is kept on its default branch and no run ever writes to it. Workspaces
are forked from it and are disposable.

Two things this makes real, which the copy mechanism did not:

- **The original folder stays authoritative.** People keep editing it directly,
  and syncing it into the mirror is the same job as fetching from GitHub with a
  directory as the upstream rather than a URL: copy the folder in, `git add -A`,
  and commit when anything changed. Deletions come along, the default branch
  becomes a history of the folder, and the sync runs on the same timer and the
  same lock as a remote fetch.
- **The return trip is explicit.** A thread's branch diverges from that history
  like any other, and applying its work means copying the reviewed result back
  out to the folder. That step is deliberate, not automatic — there is no `git
  push` to a directory nobody agreed to have rewritten.
- **Git is a poor store for large binaries.** A documents folder is fine; a folder
  of datasets or media will bloat the mirror, since git keeps a full copy per
  version for formats it cannot delta-compress.

## Materialization

The mechanism depends on what the host supports, detected once at startup and
logged. No filesystem is required: the fastest available mechanism is chosen and
the rest are fallbacks, so a plain ext4 server works, just less cheaply. The
contract is the same in every case: given a thread and a project, produce a
directory; later, release it.

| Host                      | Mechanism                  | Cost (measured, 78 MB tree)     |
| ------------------------- | -------------------------- | ------------------------------- |
| APFS, btrfs, XFS          | `cp -c` / `cp --reflink`   | 0 bytes, 0.03 s                 |
| ext4                      | `git clone <mirror>`       | objects hardlinked; tree copied |
| Linux with overlay bwrap  | `bwrap --overlay`          | 0 bytes; upperdir is the diff   |

The first two are built. The overlay is not, and not for want of a filesystem:
an overlay mount exists only inside the bwrap child's mount namespace, so there
is no directory to hand a process Octomate did not itself wrap. It becomes
possible once the runtimes are launched under a sandbox of our own, which is a
different unit than a copy mechanism.

A copied repo is a complete independent repo — its own `.git`, its own object
store. That is the requirement a `git worktree` fails: a worktree's `.git` points
back at the main repo, so every commit writes outside the workspace and Codex's
`workspace-write` sandbox refuses it.

After the copy, the workspace checks out `octomate/thread-<id>`.

The mirror is copied whole, including any uncommitted state, which is why a run
must never write to it and why the operator's own checkout is not a mirror.

### Dependencies

No fork re-downloads what another fork already has. Two mechanisms, and the first
matters more than the second.

**A shared package cache, always.** `uv`, `pnpm`, and `npm` all keep a global,
content-addressed store and hardlink out of it, so an install in a fresh workspace
fetches nothing over the network and costs almost no disk. The store is immutable
and safe to share across every workspace at once. This alone satisfies "never pull
the same dependency twice", and it needs no configuration beyond leaving the
caches at their default paths and not isolating them from the workspace.

**A warm mirror, where the tree survives copying.** Keeping dependencies installed
in the mirror means `cp -c` hands each fork a ready-to-run tree at zero disk and
zero time — better than the setup script the hosted products re-run per task.

`node_modules` copies cleanly. **A Python `.venv` copies only if it was created
`--relocatable`, so the mirror's is.** An ordinary uv venv writes an absolute
interpreter path into every console script, so a copied `.venv/bin/<tool>` keeps
running the *mirror's* interpreter and importing the *mirror's* site-packages.
Nothing errors — the workspace simply is not the environment in use.

This draft answered that with "a copied workspace re-runs `uv sync`", which does
not work: the copy already satisfies the lockfile, so uv does nothing and the
absolute shebangs stay (measured against uv 0.8.13 — `uv sync` returned in 46 ms
and changed nothing). `uv venv --relocatable` is the answer instead. Its scripts
resolve the interpreter beside themselves, uv records the choice in `pyvenv.cfg`
and later syncs keep it, and a copied venv then reports the fork's own
`sys.prefix` and imports the fork's own site-packages. So the install for a uv
tree is two commands, `uv venv --relocatable` then `uv sync`, and a fork inherits
a working environment rather than rebuilding one.

**Where each ecosystem's knowledge lives.** A `PackageManager` per manager — uv,
pnpm, npm — claiming a tree by its lockfile and owning the commands that follow.
They differ in more than the binary's name, which is why this is a hierarchy and
not a table: uv needs two commands and a flag to produce an environment that
survives being copied, where the node managers need one and would be broken by
the `ci` their own documentation recommends. What is common — whether to install
at all, and what to record afterwards — sits above them and is the same for all.

**When an install runs.** The tree records the lockfile it was last installed
from, in `.git/octomate-installed`. A mirror installs when that moves, which is
the only time it needs to — the freshness window starts at zero, so every fork
syncs, and installing on each would rebuild an environment nothing had changed.
The fork then makes the same call, and the stamp's location answers it: `cp -a`
brings the stamp along with the trees it describes, so a copied fork does
nothing, while `git clone` builds a fresh `.git` carrying no untracked file, so
an ext4 host installs its own out of the store the mirror warmed. A fork checked
out on a ref whose lockfile differs installs either way.

An install that fails is logged and the tree is handed over uninstalled. It is a
working checkout regardless and whatever runs there installs for itself, the way
every run did before this existed; a host without `pnpm` should lose its
dependencies, not its workspace.

A warm mirror also constrains sync: it must never `git clean`. It does not —
`commit_folder` stages the folder through a scratch index and finishes with
`reset --hard`, which deletes what is tracked-but-gone and leaves untracked state
where it is.

## Who gets one

- Only a **registered user** may bind a thread to a project — a `UserProfile`
  whose `user_id` is set. A visitor profile cannot.
- Only a **project-bound thread** materializes a workspace that is *kept*.
- Every other thread gets the disposable chat workspace below.

**A registered user may bind any registered project.** Octomate does not check
whether that person can read the repository on GitHub, so being in `users.yaml`
grants read access to the code of every registered project through the agent. The
mirror is fetched with the host's credential, which means Octomate's registry —
not GitHub's permissions — decides who sees what.

This is a deliberate choice for a small circle where everyone is trusted, and it
is recorded here because it is invisible from the outside: nothing fails, nothing
warns, and a person who cannot clone a repository can still read it by asking. It
stops being acceptable the moment the circle includes someone who should not see
everything.

The upgrade path is a check at bind time, in one place: ask GitHub, with the
requesting person's own grant from `octomate/oauth/github.py`, whether they can
read the repository, and refuse the bind if not. That keeps GitHub authoritative
and avoids a second copy of permissions that drifts. It costs every user having
connected their GitHub account, which is why it is not the starting point.

Note that binding is not the only surface. Project names and descriptions are
shown to the model for choosing between projects, so the registry itself reveals
what exists, and any view of another thread's runs or messages reveals code
regardless of workspace rules. Those want the same answer whenever this one
changes.

## Choosing the project and the ref

Materialization itself is runtime machinery, not something an agent decides —
given a thread and a project there is exactly one correct workspace. But two of
its inputs are genuine judgment calls, and both belong to whoever is asking:
**which project this thread is about**, and **which ref to start from**. The
default branch is the obvious answer and the wrong one often enough — continuing
someone's feature branch, reproducing against a tag, working from a PR head.

So this is the gateway's `teleport` with a `project`, which the agent calls with
typed arguments, while Octomate keeps everything that is policy:

| The agent supplies | Octomate decides                                |
| ------------------ | ----------------------------------------------- |
| project            | the path, `.octomate/workspaces/<thread_id>`    |
| ref (optional)     | the mechanism — `cp -c`, clone, or overlay      |
|                    | whether this user may bind this project         |
|                    | serialization against a concurrent materialize  |
|                    | the branch it lands on, and its lifecycle       |

Two constraints that are easy to miss:

**A thread binds once.** Re-binding is refused: a thread that already has a
project keeps it for life, and calling the capability again is an error rather
than a switch. This removes the case where an agent calls twice and silently
destroys the first workspace's contents, and it keeps a thread's history honest —
what a thread is about does not change underneath the record of what it did. A
different project is a different thread.

**It takes effect by ending the turn.** A run's working directory and sandbox
policy are fixed when the process spawns, so a workspace created mid-turn cannot
become the current run's cwd — and a model told to wait would go on working in
the empty tree it started in, whose contents this run is the last to see. So
binding is a `teleport` with a `project`: Inkling's run ends on the deferral,
and a driven Claude or Codex turn is interrupted the moment the session records
the decision and ends as the same deferral. The graph resolves it at once and resumes
the agent on the same conversation — now in the project's workspace, the call
answered by a line saying so — and the model carries on where it left off.
Claude files a session's transcript under the cwd it ran in and resumes only
from there, so the graph has the tentacle carry the transcript to the workspace's
own project dir before the run resumes.

This is also the path by which a chat thread becomes a project thread: someone
says what they want worked on, a `teleport` with the project binds it, and the
resumed run starts in
the project's code, in a workspace whose work is kept.

**The agent may also give the workspace back.** A project thread's tree is kept
between turns and swept only once it has sat idle, so a thread whose work is
finished — merged, delivered, dropped — holds disk it has no claim on. `dispel`
is the agent saying so: the gateway records it, and when the turn ends the graph
saves the turn as always and then releases the tree, under the sweep's own rule
that a workspace holding work the mirror has not seen is kept. Nothing is lost —
a later message on the thread forks it afresh and lays the ref back over it —
and nothing is pulled out from under a run still in it, which is why the release
waits for the turn rather than happening in the call.

**Every driven runtime can call it.** `teleport` with a `project`, `dispel`, and
the `projects` facet of `scry` are gateway spells: Inkling mounts them as a capability, a driven
Claude turn as its in-process MCP server, a driven Codex turn over the served
`/octomate/mcp`. A native session may scry the projects but neither teleport nor
dispel — its work is wherever its terminal is, and there is no tree to fork for
it (a native teleport, still to come, is the door for that). dsh mounts no gateway on a driven
turn, so a chat thread
there still has a workspace it may write to, whose work is thrown away, and no
way to say what it is about: `dsh web` is one daemon serving every thread, with
no per-thread process to attach a tool to.

## The chat workspace

A process always has a working directory, so a thread with no project still needs
one. It used to fall back to the agent's configured `cwd`, which defaults to
`"."` — on a server, Octomate's own install directory.

Instead: a workspace of its own, forked the ordinary way from a prepared empty
repository at `mirrors/.blank`, and thrown away when the run that made it ends.

**One code path, not two.** Both kinds are a fork of a mirror, made by the same
`materialize`, landing on the same `octomate/thread-<id>` branch, writable, and
bounded by the same `sandbox: workspace_write`. What binding changes is the
ending — a project thread's tree is saved to its mirror and resumed into next
turn, a chat thread's is discarded. That is a better thing to tell a model than
"you may not write": **binding is what makes your work kept.**

A run asks `WorkspaceManager.open` for the workspace its thread runs in and enters
it. The handle knows its path without touching the disk, so a runtime can be
configured with the directory it will run in before the process exists; entering
forks the tree, and leaving ends it — `ProjectWorkspace` keeps what is there,
`ChatWorkspace` throws it away. The mirror side takes the same shape: the blank
repository is `MirrorManager.create()` with no project — the mirror of nothing,
made and located exactly as every other mirror is.

This replaces one shared read-only directory, which was the earlier design and
shipped briefly (`e80b288`, `f119e87`). Sharing was safe only because nothing
could write there, so every capability a conversation might want had to be taken
away to keep one property true — and two of the three runtimes could not enforce
it anyway (Claude's hook scoped the file tools and left Bash open; dsh ships no
read-only preset at all). A fork each keeps the property by construction, and
costs nothing, because a fork of an empty repository is a fork of nothing.

**Nothing survives the turn.** "Write me a script" followed by "now run it" fails:
the second turn forks a fresh empty tree. The conversation itself persists —
Claude resumes its session, Codex its thread — but the filesystem does not. Worth
saying in the model's instructions rather than letting it discover the file is
gone.

**The path is the thread's, even though the tree is the run's.** Both runtimes key
a resumable session by the directory it ran in: Claude stores transcripts under
`~/.claude/projects/<cwd-slug>/`, and `--resume <id>` from anywhere else answers
"No conversation found with session ID" (verified against CLI 2.1.221). A
per-run directory name would therefore cost every chat thread its memory. So the
fork lands at `workspaces/chat/<thread_id>` every time, and what makes it this
run's is that it is emptied and re-forked, not that it is named after the run.

Two overlapping runs of one conversation — a mid-run follow-up supersedes the turn
before it has finished — share the tree while they overlap, counted so the first to
leave does not delete the floor from under the second.

The directory must still be set explicitly. Leaving it unset means inheriting
Octomate's own working directory, and a writable run parked there is worse than a
read-only one: `.octomate/` holds the database, `users.yaml`, `providers.yaml`,
and service account keys.

What a run may do once it is there is the runtime's own answer, not Octomate's:
Claude's permission mode, Codex's `sandbox` preset, dsh's bundled one. Octomate
says where a run happens and relays what the agent decides; it adds no boundary
of its own, so a runtime that lets a write out of its `cwd` will let one out
here too.

## Lifecycle

The workspace is a cache, not the only copy. That is what makes reclaiming it a
disk decision rather than a data-loss decision.

- **Each turn** snapshots the whole tree — staged, unstaged and untracked alike —
  and pushes that to the mirror under `refs/octomate/threads/<id>`. Pushing to a
  ref namespace rather than `refs/heads/` keeps `git branch` clean and keeps the
  refs out of ordinary clones. Nothing reaches GitHub until a human asks for a PR.

  A snapshot, not a commit, and that distinction is the whole of it. The snapshot
  is built with `commit-tree` over a copy of the index, so HEAD does not move, the
  branch keeps only the agent's own commits, and a workspace in the middle of
  something is not finished on its behalf. A machine `wip` on the end of every
  turn would otherwise be buried inside the history a person reviews. The commit
  object still exists — a push needs one — but no branch points at it, exactly as
  no branch points at a stash. `git stash create` is the obvious tool and the
  wrong one: it takes no options, so it captures tracked changes only, and an
  agent's first turn is mostly new files.
- **Resuming** puts the workspace back the way the turn left it: the branch to the
  snapshot's parent, the tree from the snapshot, then the index back to HEAD so
  uncommitted work reads as uncommitted. Modifications, deletions, untracked files
  and a detached HEAD all make the round trip. What does not is the staged/unstaged
  split, since one tree cannot hold both, and `.gitignore`d files — a rebuilt
  workspace's `.venv` is the one the fork installs, not the one the turn was using.
  Reconciling the history that comes back is the agent's job, not Octomate's.
- **Pruning** happens on an idle timer. Being wrong costs a slow resume, never
  lost work, so the heuristic does not need to be good. A thread with a pending
  `DeferredAction` is known to be alive and is a reasonable last choice to evict,
  but that only orders eviction — it never blocks it.
- **Dispelling** is the same release on the agent's word instead of the timer:
  saved first, and kept if the save did not take.
- The chat workspace is never pruned. It is empty.

## Mirror sync

The open question this draft was written to answer.

A no-op sync is one connection round trip to GitHub; the data is negligible, since
a mirror that is already current transfers nothing. The cost is therefore whatever
connection setup costs from wherever Octomate runs, and it is paid per thread
creation, not per turn. That needs measuring on the server rather than a
workstation, where a VPN dominates the number and makes it meaningless.

Cost is not the deciding argument anyway. **Availability is**: fetching on fork
couples starting a thread to GitHub being reachable, so an outage or a rate limit
stops people from working on a repo the server already has. Bursts matter too —
ten threads opened at once should not mean ten fetches of one mirror.

**A freshness window, starting at zero.**

- Materialization fetches unless the mirror was synced within the window.
- **The window starts at 0**, so every fork fetches. Raise it once there is a
  real number from the server to raise it against.
- If a fetch fails and a mirror exists, materialize from it and say it is stale.
  Never silently.
- If no mirror exists, fail. There is nothing to fall back to.
- Two materializations racing on one mirror serialize on a lock keyed by mirror.

Starting at zero costs nothing in robustness, because the availability concern is
already handled by the rule above it: a failed fetch degrades to a stale mirror
with a warning rather than blocking the thread. So the window is purely a
performance knob, and there is no reason to guess at its value before measuring.
Raising it above zero adds a background sync on a timer; nothing else changes.

`--prune` follows upstream branch deletions. Thread refs live outside the fetched
refspec, so pruning never touches them.

**Which credential fetches.** The machine's own, ambient: Octomate runs `git
fetch` in the mirror directory and inherits whatever git credential the host user
has. No token handling, no configuration of its own, and the same code path on a
workstation as on the server.

That is sufficient because a mirror fetch is a read, and a read needs no
attribution. The attribution argument applies to work — whose rate limit, whose
name on the result — and fetching produces none. Nor is the credential the access
boundary: the project registry is. An unregistered repository cannot be worked on
no matter what the host key can technically reach.

Two things to get right from the start, because both are expensive to retrofit:

- **The fetch credential must be read-only** — a per-repository deploy key with
  write access off, or a host key kept read-only. If it can push, eventually
  something will push with it, and every PR will come from the machine.
- **Pushing upstream for a PR uses the requesting person's credential**, through
  the existing per-user GitHub grant, so the PR is authored by them.

The credential must also never block on input. A passphrase-protected key with no
agent, or a prompt for a password, leaves `git` waiting on stdin and the fetch
looks hung rather than failed — the same failure shape as `codex exec` outside a
git repo. Set `GIT_TERMINAL_PROMPT=0` and SSH `BatchMode=yes` so it fails fast.

A GitHub App installation token is the upgrade path, not the starting point. It
buys a central repository allowlist, short-lived credentials, and an identity that
is not a person's — worth it when deploy keys outgrow being managed by hand, or
when the org wants audit. It is not worth registering an app, holding a private
key, and refreshing tokens hourly for a handful of repositories.

## What `Project` needs

In the order a review reads it: schema and model, then the manager, then call
sites. The migration is autogenerated from the model, as always.

**Add `upstream`.** Not an optional URL — the two kinds have different fields and
different behavior, so they are variants:

```python
class RemoteUpstream(BaseModel):
    kind: Literal["remote"] = "remote"
    url: str


class DirectoryUpstream(BaseModel):
    kind: Literal["directory"] = "directory"
    path: LocalPath
```

A remote upstream makes its mirror by cloning and syncs by fetching; a directory
upstream makes its mirror by `git init` and syncs by copying in and committing.
One field with a `None` for the other case would put that branch in the code
instead of in the type.

**Remove `origin`, and `ProjectOrigin` with it.** The field's own comment says it
is stored because it is "the only part of a discovered project that cannot be
recovered later". Nothing is discovered any more: every project is declared, and
Codex reuses the registry rather than adding to it. The justification goes when
discovery goes, and `octomate/types/projects.py` has nothing else in it.

**`ProjectManager.ensure` goes; every caller uses `resolve`.** The three Codex
sites that call `ensure(..., origin="codex")` — in `tailer`, `ingest`, and
`base` — become lookups. A session whose cwd matches no declared project is filed
with no project, which is already a supported state. This deletes the
two-kinds-of-caller distinction that `ProjectOrigin`'s comment exists to explain,
along with `ensure_lock` and the registration race it guards.

**`root` keeps the field and changes jobs.** It stops being where runs happen and
becomes how an ambient session is recognized: someone running `claude` by hand in
`~/Projects/inky` still has to be attributed, and matching cwd against `roots` is
how. Location moves to the workspace; recognition stays here.

**`deny_outside_project` must bind to the workspace, not the project.** It is
currently `partial(deny_outside_project, project)` and tests `project.contains()`.
A workspace lives under `.octomate/workspaces/`, which is not below any project
root, so leaving this alone means every write in the new model is refused by the
project's own boundary.

**Derived rather than stored:** the mirror path follows from the name, and the
default ref is the mirror's `origin/HEAD`. Storing either creates a second copy of
something already knowable.

Still undecided here: what `extra_roots` means once a project is one mirror —
whether a sibling tree is forked too, or is only ever recognition. How a workspace
learns its install command is settled: the lockfile says, and a field waits until
a project turns up that needs one.

## Open

1. **The freshness window's real value.** Zero until the cost of a fetch is
   measured on the server, where a workstation VPN is not distorting it.
2. **Large binaries in a locally-initialized mirror.** A documents folder is
   fine. A folder of datasets or media has no answer yet, and the mirror is where
   it will show up.
3. **How a reviewed change returns to a locally-mirrored folder.** The copy-back
   is deliberate by design, but who performs it, and what it looks like in a
   channel, is unspecified.
