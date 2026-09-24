# Releases

Three distributions release independently through one Release Please pull request:

| Package | Source | Tag |
|---|---|---|
| `octomate` | `octomate/` and the root project metadata | `octomate-vX.Y.Z` |
| `octomate-cli` | `cli/` | `octomate-cli-vX.Y.Z` |
| `octomate-protocol` | `protocol/` | `octomate-protocol-vX.Y.Z` |

Pushes to `main` that change package sources, metadata, deployment files or release
configuration update the release pull request. Release Please assigns commits by
path and bumps only the affected packages. The server's detection excludes the
CLI and protocol packages, tests, docs, Trunkline, CI and editor configuration, and
agent instructions; see `exclude-paths` in `release-please-config.json`.
Squash commit titles follow Conventional Commits, and
before 1.0 a breaking change bumps the minor version while features and fixes bump
the patch.

The release workflow runs `uv lock` on the release branch and commits the lockfile
when it changes, which triggers the normal checks. After the merge, Release Please
creates a GitHub release and tag per changed package, the checks run against the
tagged commit on Python 3.12 and 3.13, the distributions are built and installed
into throwaway client and server environments, and one publishing job per released
package uploads to PyPI through a trusted publisher. Wait for every publishing job
before announcing a release; a failed one is rerun from the same workflow run.

## Pull requests and squash merges

Merge pull requests with **Squash and merge**. The repository disables merge
commits and rebase merges. The squash commit title defaults to the PR title, and
its body defaults to blank. Keep those defaults: each PR should contribute one
release entry. Including the branch's commit messages can repeat entries in the
changelog.

The PR title describes the final change and determines its release classification.
Use `<kind>: <description>` or `<kind>(<scope>): <description>`. Allowed kinds are
`feat`, `fix`, `docs`, `chore`, `refactor`, `test` and `perf`; the description starts
with a lowercase letter. The scope is optional. For example:

```text
fix(cli): wait for macOS process reaping during restart
```

For a breaking change, add `!` immediately before `:`, with or without a scope:
`feat(protocol)!: require the new handshake format`. Put the explanation and
migration steps in the PR description and the relevant documentation; the squash
commit body stays blank, so the title must carry the breaking-change marker.

The [PR title check](https://github.com/kalynnka/octomate/blob/main/.github/workflows/pr-title.yml)
enforces this format. Correct the title and wait for that check to pass before
merging, then confirm the squash commit title still matches the PR title and its
body is empty.

## Compatibility

The CLI installs without the server, and internal dependencies declare compatible
ranges rather than pinned versions. A compatible server update needs no client
release: the transcript stream checks a wire protocol number at the handshake, not
package versions, and refuses a mismatch with a line naming both. A breaking wire
change bumps that number and the dependent ranges together. Hooks and the MCP
endpoint are separate contracts and stay backward compatible for older clients.

## Deploying

Publishing changes nothing on a running server. On macOS, `octomate service
upgrade` fetches the latest stable server tag, backs up, migrates and restarts;
elsewhere the steps are manual. See [Upgrades and
backups](../installation/upgrading.md).
