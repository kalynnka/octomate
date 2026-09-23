# Checks and tests

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
