# Register projects

Register repositories or folders that agents may work on. Users can then select a
project in Trunkline or ask an agent to use it. The [usage guide](../usage/projects.md)
covers choosing a project and reviewing the result.

## Add a project

In `projects.yaml` under your [config home](configuration.md):

```yaml
projects:
  website:
    root: /srv/projects/website
    upstream:
      kind: remote
      url: git@example.com:team/website.git
    description: The team website and its documentation.
  notes:
    root: /srv/projects/notes
    upstream:
      kind: directory
      path: /srv/projects/notes
    description: Shared notes and drafts.
```

Replace these paths and the repository URL with your own. A directory project
can use an ordinary folder; Octomate does not turn the original folder into a
Git repository.

| Field | What to supply |
|---|---|
| `root` | An existing absolute directory on the server. Native sessions reported from that machine can be associated with it. |
| `upstream` | A Git URL with `kind: remote`, or a source folder with `kind: directory`. Required. |
| `description` | A short description to help users and agents choose the project. Keep agent instructions in the project's own instruction files. |
| `extra_roots` | Additional directories belonging to the project. Claude Code can use these as additional working directories. |
| `enabled` | Defaults to `true`; a missing root causes the project to be disabled during reconciliation. |

Use a distinct project name for each checkout you want to offer.

!!! warning "Projects are shared with registered users"
    Any registered user can select an enabled project. Octomate does not check
    that user's access to the upstream repository. Register only content that
    everyone with an account on this deployment may access.

## Check access and restart

For a remote project, make sure the server account can fetch the repository
without an interactive credential or SSH passphrase prompt. A read-only
credential is enough to prepare workspaces; publishing changes requires separate
write access if you want agents to do it.

[Validate the configuration](configuration.md#validate-without-starting), restart
Octomate, and check that the project appears when starting a conversation in
Trunkline. Select it and ask the agent to inspect the project without changing it.

Workspaces save changes independently of the source. They do not automatically
push to the repository or copy changes back into a directory project. See
[reviewing and delivering work](../usage/projects.md#review-and-deliver).
