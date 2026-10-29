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

## Debug native attachments

In this checkout, the VS Code tasks **Octomate: debug attachment on** and
**Octomate: debug attachment off** switch native Codex and Claude Code hooks and
user MCP entries together. The off task uses the installed standalone CLI;
upgrade it separately with `octomate upgrade`. Restart the runtime so it reads
the new MCP entry. A Claude MCP entry installed with `local` or `project` scope
overrides the user entry; remove it with `octomate claude mcp uninstall` using
its scope from that project if it should follow the switch.
