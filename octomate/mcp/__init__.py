"""What Octomate serves as MCP: one FastMCP server per tool family, each at
`/<name>/mcp` behind the deployment's one secret.

The servers are built where their dependencies live (`Octomate.mcp_servers`);
`base` holds what serving them shares, and each family has its own module —
`gateway` for the routing spells. Policy stays with the managers; nothing here
decides anything."""
