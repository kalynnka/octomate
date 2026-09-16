"""Provider OAuth: configured flows in `flows`, MCP discovery in `mcp`.

A flow owns only what its provider does differently — endpoints, token exchange,
scope and account discovery — and is composed into an `OAuthConnector` by whoever
builds the application. What every integration shares stays in its own central home:
`managers.oauth` registers connectors and owns the user authorization boundary,
`schemas.oauth` holds the flow protocols, transports and persisted connection, and
`models.oauth` their tables.
"""
