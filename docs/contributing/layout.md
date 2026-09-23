# Repository layout

```text
octomate/
  base.py            Octomate: the coordinator, also the FastAPI app
  app.py             the application factory: config in, tentacles connected
  reflex/            the graph: nodes, state, the suspender
  tentacles/         agent, channel and MCP base classes; one package per platform
  capabilities/      tools an agent is given: gateway, ask, todos, history, MCP
  managers/          everything that touches the database
  schemas/           Arcanus transmuters: the domain types
  models/            SQLAlchemy models behind them
  config/            settings, and the packaged defaults
  mcp/               the served MCP server
  oauth/             device and authorization-code flows
  migrations/        Alembic
cli/octomate_cli/    the operator CLI and the hook clients
protocol/            the wire contract both sides depend on
trunkline/           the web console
```
