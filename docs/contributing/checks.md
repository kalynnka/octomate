# Checks and tests

Python checks run when `octomate/`, `cli/`, `protocol/`, shared tests, build inputs
or the checks workflow change. They test and build all three distributions
together: the server depends on the CLI and protocol, and the CLI depends on the
protocol. This catches compatibility problems in their consumers too.

CI runs pytest with two worker processes and reports the 25 slowest test phases.
Use `uv run pytest -n 2 --dist worksteal --durations=25` to reproduce that run
locally; plain `uv run pytest` remains sequential. Docs and Python checks use
separate uv cache keys so a docs-only installation cannot occupy the package
checks' cache.

Trunkline has a separate workflow for changes under `trunkline/` or its workflow
file. Docs build on every pull request targeting `main` and every push to `main`.
Manual runs of Checks and Trunkline validate their respective parts regardless
of the changed paths. The release workflow calls both at the release commit
and requires both to pass before publishing. PRs have no release-only Trunkline
job or documentation deployment job.

```sh
uv run pytest                                  # everything except live replays
uv run ruff format <paths> && uv run ruff check <paths>
uv run pyright <paths>                         # basic mode, as the editor runs it
```

Ruff's configured rule set is what "clean" means, and CI runs it only on changed
files, so pass paths explicitly rather than reformatting the tree. Pyright runs in
basic mode; a suppression needs a reason from the three `AGENTS.md` allows.

Tests run with an in-memory database URL so the suite cannot reach a real one, and
each test gets its own working directory so workspace and mirror state cannot leak
into the checkout. A test that needs rows uses the `in_memory_engine` fixture and
the parents that foreign keys require: `tests.support.managers.a_thread`,
`a_user`, `a_project`. Live replays against a real chat platform sit under
`tests/trigger/` behind the `trigger` marker and a gitignored `trigger.yaml`, and
run only when named.

`tests/support/` holds the fakes the suite is built on: a real `ChannelTentacle`
subclass over a recording ink, a scripted agent at the tentacle level and at the
model level, in-memory manager fakes, and the canonical run scenarios every
rendering test replays.
