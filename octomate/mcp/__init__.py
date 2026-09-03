"""What Octomate serves as MCP: one FastMCP server at `/gateway/mcp` behind the
deployment's known bearers, composed in `server` from a module per tool family —
`gateway` for the routing spells, `history` for the thread ledger.

The server is built where its dependencies live (`Octomate.mcp_servers`); `base`
holds what serving shares. Policy stays with the managers; nothing here decides
anything."""
