# Set up

```sh
git clone https://github.com/kalynnka/octomate.git
cd octomate
uv sync
uv run alembic upgrade head          # a development database under .octomate/
uv run octomate service serve --reload
```

`uv sync` installs the runtime, development and documentation groups. The
development database is `.octomate/octomate.db` in the checkout; the test suite
never touches it.

Trunkline is a separate build:

```sh
cd trunkline && pnpm install && pnpm dev
```
