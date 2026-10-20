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

Each test has a 180-second timeout, including setup and teardown. A stalled test
fails with thread stacks instead of blocking the suite. Each CI unit-test job
also has a 15-minute limit.

Trunkline has a separate workflow for changes under `trunkline/` or its workflow
file. Docs build on every pull request targeting `main` and every push to `main`.
Manual runs of Checks and Trunkline validate their respective parts regardless
of the changed paths. The release workflow calls both at the release commit
and requires both to pass before publishing. PRs have no release-only Trunkline
job or documentation deployment job.

```sh
uv run pytest                                  # everything except live checks
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
`a_user`, `a_project`. Unit tests mock model providers and channel transports.
Live model calls and chat replays sit under `tests/trigger/` behind the `trigger`
marker and run only when named. They use the deployment's configuration; chat
replays also need a gitignored `trigger.yaml` naming their destination.

To preview artifact cards in Slack, Lark, Discord, and NapCat, configure a saved
artifact in the same file alongside the channel destinations:

```yaml
trigger:
  napcat:
    chat_id: "123456789"
    user_id: "123456789"
  artifact:
    artifact_id: "019a0000-0000-7000-8000-000000000001"
    title: "Library diagram"
    provider_name: "Diagrams"
    viewer_url: "https://console.example.com/artifacts/019a0000-0000-7000-8000-000000000001"
```

Replace the sample metadata and URL with an existing artifact from your deployment.
The NapCat example targets a private chat; for a group, add `chat_type: group`, set
`chat_id` to the group number, and keep `user_id` as your QQ number. The channel must
also be enabled and reachable through its configuration in `tentacles.yaml`.

From the repository root, `uv run pytest tests/trigger -k artifact` posts a run
notice and artifact preview in each configured channel. Missing channel or artifact
configuration skips that preview. These checks send saved overview metadata only;
they do not create artifacts or run MCP tools. Open the card's button, or NapCat's
text link, while signed in as the artifact owner to inspect the viewer. The shared
browser window and Trunkline embed have separate automated coverage: from
`trunkline/`, run `pnpm test:browser` for the isolated Firefox suite.

Run `uv run pytest tests/trigger/test_inkling.py` to check that the configured
DeepSeek model returns structured replies and mentions. It uses fictional QQ
identifiers and sends nothing to a chat platform.

`tests/support/` holds the fakes the suite is built on: a real `ChannelTentacle`
subclass over a recording ink, a scripted agent at the tentacle level and at the
model level, in-memory manager fakes, and the canonical run scenarios every
rendering test replays.
