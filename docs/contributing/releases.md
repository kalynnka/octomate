# Releases

Three distributions release independently through one Release Please pull request:

| Package | Source | Tag |
|---|---|---|
| `octomate` | `octomate/` and the root project metadata | `octomate-vX.Y.Z` |
| `octomate-cli` | `cli/` | `octomate-cli-vX.Y.Z` |
| `octomate-protocol` | `protocol/` | `octomate-protocol-vX.Y.Z` |

Pushes to `main` update the release pull request. Release Please assigns commits by
path and bumps only the affected packages; the server's detection excludes `cli/`,
`protocol/`, `tests/` and `docs/`. Commit subjects follow Conventional Commits, and
before 1.0 a breaking change bumps the minor version while features and fixes bump
the patch.

The release workflow runs `uv lock` on the release branch and commits the lockfile
when it changes, which triggers the normal checks. After the merge, Release Please
creates a GitHub release and tag per changed package, the checks run against the
tagged commit on Python 3.12 and 3.13, the distributions are built and installed
into throwaway client and server environments, and one publishing job per released
package uploads to PyPI through a trusted publisher. Wait for every publishing job
before announcing a release; a failed one is rerun from the same workflow run.

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
