"""What Octomate serves as MCP: one FastMCP server at `/octomate/mcp` behind the
deployment's known bearers, composed in `server` from a module per tool family —
`gateway` for the routing spells, `history` for the thread ledger, `oauth` for
linking a person's accounts with the providers whose tools the server proxies.

The server is built where its dependencies live (`Octomate.app`); `base`
holds what serving shares. Policy stays with the managers; nothing here decides
anything."""
