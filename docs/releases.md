# Releases and publishing

Octomate uses one Release Please release PR with independent versions, changelogs,
and tags for its three PyPI distributions. Only packages with release changes are
published; a server update does not require a CLI or protocol release.

| Package | Source | Changelog | Tag |
| --- | --- | --- | --- |
| `octomate` | `octomate/`, root project metadata | `CHANGELOG.md` | `octomate-vX.Y.Z` |
| `octomate-cli` | `cli/` | `cli/CHANGELOG.md` | `octomate-cli-vX.Y.Z` |
| `octomate-protocol` | `protocol/` | `protocol/CHANGELOG.md` | `octomate-protocol-vX.Y.Z` |

## Compatibility

The CLI remains installable without the server. Internal dependencies declare
compatible ranges, currently `>=0.0.1,<0.1`, rather than matching package versions.
Release Please updates a package's own version, leaving these ranges unchanged.
Raise a minimum only when a consumer needs a newer dependency's behavior.

Clients do not need to update when the server releases a compatible change. The
stream handshake checks `STREAM_PROTOCOL`, not the CLI or server package version;
`client_version` is diagnostic. For example, CLI `0.0.2` can stream to server
`0.0.8` when both speak stream protocol `1`.

Keep changes within the current protocol series backward compatible. A breaking
wire change must increment `STREAM_PROTOCOL`, release a new protocol series, and
update the affected consumers' dependency ranges and implementations. Before 1.0,
breaking changes bump the minor version; compatible features and fixes bump the
patch version. The stream server currently accepts one wire version and refuses a
mismatch explicitly. Hooks and MCP have their own existing contracts and must also
remain backward compatible for older clients.

## Account setup

Complete these steps after reviewing the prepared repository changes, before
merging the release workflow into `main`.

1. In GitHub, create a fine-grained personal access token for this repository with
   Contents, Pull requests, and Issues read/write permissions. Add it as the
   repository Actions secret `RELEASE_PLEASE_TOKEN`. A GitHub App installation
   token with equivalent permissions can be used instead if generated in the job.
   The workflow requires the configured token so release PRs and the lockfile
   commit trigger normal PR checks; it does not fall back to `GITHUB_TOKEN`.
2. Enable Actions for the repository and allow GitHub Actions to create pull
   requests under Settings → Actions → General. Create the GitHub environments
   `pypi-protocol`, `pypi-cli`, and `pypi-server`.
3. Sign in to PyPI and configure a Trusted Publisher for **each** of these names:
   `octomate-protocol`, `octomate-cli`, and `octomate`. If a project does not exist,
   use a pending publisher under account Publishing to create it on first upload.
   The account must be able to publish under all three names.

   | PyPI publisher field | Value |
   | --- | --- |
   | Owner | `kalynnka` |
   | Repository | `octomate` |
   | Workflow filename | `release.yml` |

   Use the matching environment for each project:

   | PyPI project | Environment |
   | --- | --- |
   | `octomate-protocol` | `pypi-protocol` |
   | `octomate-cli` | `pypi-cli` |
   | `octomate` | `pypi-server` |

   PyPI requires distinct publisher configurations for pending projects. Separate
   environments let all three projects be created by their first automated release.
   No PyPI API token is needed: each publishing job obtains short-lived credentials
   through OIDC.
4. Use Conventional Commit PR titles and squash merges so the release version
   reflects the merged changes. Require the PR title and both Python check jobs
   in the repository's branch rules once they have run successfully.

See [Release Please credentials](https://github.com/googleapis/release-please-action#github-credentials)
and [PyPI pending publishers](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Release flow

Pushes to `main` update the release PR. Release Please assigns commits by package
path and updates only the affected versions and changelogs. The root server
excludes `cli/`, `protocol/`, `tests/`, and `docs/` from its change detection;
changes to shared root files can still warrant a server release. Commit scopes
are descriptive, not the routing mechanism.

The release workflow then runs `uv lock` on the release PR branch
and commits only `uv.lock` when needed. That commit triggers a fresh check run.
Review and merge the release PR after its lockfile is synchronized and checks pass.

Each package's version manifest starts at `0.0.1`. Release Please calculates its
next version from Conventional Commits with the pre-1.0 policy above. There is
no need to create or push a version tag manually.

After the release PR is merged:

1. Release Please creates a GitHub release and component tag for each changed package.
2. Checks run against the tagged commit on Python 3.12 and 3.13. They run the test
   suite, validate the lockfile, build all wheels and source distributions, and
   install the artifacts into temporary client and server environments.
3. The installation check rebuilds every source distribution, confirms internal
   dependency compatibility, verifies the client has no server dependencies, and runs
   the installed server with a disposable database and authenticated MCP check.
4. A publishing job runs for each released package, using its matching environment.
   Packages upload independently; wait for all publishing jobs to finish before
   installing a release that needs newly released dependencies. Each package's wheel
   and source distribution are attached to its own GitHub release. Building unchanged
   packages for checks does not publish them or change their versions.

Ruff and Pyright check changed Python files. The full test suite runs for every
check job; live trigger tests remain skipped. Repository-wide lint cleanup is
separate from this release preparation.

The GitHub release is created before PyPI publishing completes. Wait for the
publishing jobs to succeed before announcing package availability. If publishing
fails partway through, rerun the failed jobs in that workflow run: they reuse the
checked artifacts, and already uploaded distributions are skipped. Running a new
workflow dispatch after the tag exists does not recreate its publication jobs.

## Manual deployment

Publishing does not update a running server. Once a release is ready, run
`octomate upgrade` on an existing managed deployment. It selects the latest stable
`octomate-vX.Y.Z` GitHub release automatically, ignoring CLI/protocol tags and
drafts/prereleases. It fetches the server tag and exits unchanged when already
current. Otherwise it backs up, checks out the release commit, synchronizes the
release's lockfile, migrates, restarts and verifies the service.

The current upgrade command supports only an installed launchd/plist service and
a source checkout. A plain PyPI installation can run `octomate serve`, but it is
not a managed deployment for `octomate upgrade`. See the [deployment guide](server-deployment.md)
for bootstrap, configuration, migrations and recovery.

Tailcat remains an optional, separately managed connection step. No release job
connects to a deployment host or receives its application credentials.
