# Projects

A **project** is a code location the operator has declared. A thread bound to one
runs in its own [workspace](workspaces.md), forked from a pristine mirror of the
project, and saves its work back to that mirror after every turn.

Native sessions are unaffected. A Claude Code or Codex session you run in your own
terminal works wherever you started it; Octomate only records it.

## Declare a project

Projects live in `projects.yaml`, keyed by name. Declaring one is the operator
vouching for its contents: a registered user can then ask any agent to work in it.

```yaml
projects:
  inky:
    root: ~/Projects/octoverse/inky
    upstream:
      kind: remote
      url: git@github.com:kalynnka/octomate.git
    description: The Octomate server. Python, uv, pytest.
  notes:
    root: ~/Documents/notes
    upstream:
      kind: directory
      path: ~/Documents/notes
```

| Field | Meaning |
|---|---|
| `root` | The directory this project is, as an absolute local path. Also how a native session is recognised: a session whose working directory is under a root is attributed to that project. |
| `upstream` | **Required.** Where the mirror comes from. `remote` clones and fetches a git URL. `directory` mirrors a plain folder by copying it in and committing, so a folder of documents gets branches and history without ever becoming a repository itself. |
| `extra_roots` | Further directories that are also this project. Claude receives them as additional directories; the other agents ignore them. |
| `description` | Shown to a model choosing between projects. Not agent instructions: those belong in the project's own `AGENTS.md` or `CLAUDE.md`. |
| `enabled` | Default `true`. Cleared automatically while the root is missing from disk. |

Two checkouts of one repository are two projects. Name them after their
directories.

The mirror is fetched with whatever git credential the host user has. Make sure it
works without a prompt: Octomate runs git with `GIT_TERMINAL_PROMPT=0` and SSH in
batch mode, so a passphrase prompt fails fast instead of hanging. Prefer a
read-only deploy key, since nothing Octomate does needs to push upstream.

!!! note "Who may bind what"
    Any registered user may bind a thread to any enabled project. Octomate does not
    check whether that person can read the repository upstream, so the registry is
    the access boundary. Declare only what everyone with an account may see.

## How a thread gets a project

A thread binds to a project **once**. There are three ways in:

1. **From Trunkline.** A thread's first message can name a project. The console
   lists enabled projects.
2. **By the agent, mid-conversation.** The gateway's `teleport` spell takes a
   `project` and an optional `ref` (a branch, tag or commit to start from). The
   agent calls it when someone says what they want worked on; the run ends, the
   workspace is forked, and the same conversation resumes inside it. Only a
   registered user's request can bind. See [Moving a conversation](gateway.md).
3. **By attribution.** A native session started under a declared root is filed
   under that project, so its history reads alongside the driven threads about the
   same code. This applies to sessions reported from the server's own machine.

Re-binding is refused. A different project is a different thread.

## Delivering the work

Nothing Octomate does reaches the upstream. A snapshot goes to the mirror's local
path; the upstream sees a thread's work when someone pushes a branch and opens a
pull request. The agent can do that from inside the workspace with the host's git
credential, so the branch is authored by the machine unless the agent sets its own
identity.

For a directory-upstream project, carrying reviewed changes back to the folder is a
deliberate step nobody performs automatically. Copy what you accept.
