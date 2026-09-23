# API reference

Generated from the source by [mkdocstrings](https://mkdocstrings.github.io/).
Choose a section in the left sidebar, then a topic such as agent settings,
conversation managers or CLI commands. Each topic has its own reference page.
Use the page's table of contents to jump to its modules, classes and methods.

The guide says how to use a feature; the reference gives its Python contract.
The build reads the code statically: no server is imported, no database opened.

The running server also serves its HTTP schema at `/docs`, which reflects the
tentacles that deployment mounted.

| Section | Contents |
|---|---|
| [`octomate.config`](config.md) | Every setting: the deployment, the tentacle registry, agents, channels, MCP, auth, OAuth, projects, providers, observability |
| [`octomate.tentacles`](tentacles.md) | The tentacle base classes, the channel split into chromo, ink and feelers, and the hook guard |
| [`octomate.reflex`](reflex.md) | The graph, its state and dependencies, the nodes, and the suspender |
| [`octomate.managers`](managers.md) | Everything that touches the database: threads, conversations, actions, users, auth, projects, workspaces, MCP, OAuth |
| [`octomate.schemas`](schemas.md) | The domain types, and the literal types they are built from |
| [`octomate.capabilities`](capabilities.md) | The tools an agent is given, and the harness they run in |
| [`octomate.mcp`](mcp.md) | The served MCP server, and the OAuth flows behind connectors |
| [`octomate_cli`](cli.md) | The operator CLI, the hook clients and the transcript tails |
| [`octomate_protocol`](protocol.md) | The contract the server and the client share |

Internal APIs are still evolving. Read the source and the tests before building
on one.
