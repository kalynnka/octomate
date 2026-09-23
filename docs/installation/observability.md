# Observability

Three layers, all configured in `observability.yaml`: plain logging, Logfire tracing
for Octomate's own spans, and per-harness instrumentation that brings each agent's
native telemetry into the same trace.

## Logging

```yaml
logging:
  level: INFO
  loggers:
    httpx: WARNING
```

`loggers` sets per-logger overrides on top of `level`. Each tentacle claims its own
vendor loggers and colours its lines in the console. Provider HTTP request lines are
kept at INFO only for LLM providers.

On macOS, `octomate service logs --follow` tails the service's stdout and stderr
files. The CLI's own maintenance events, such as upgrades and migrations, go to
`logs/server.log` beside them.

FastAPI's interactive API reference is served at `/docs` on the server.

## Logfire

```yaml
logfire:
  service_name: octomate
  environment: development
  send_to_logfire: false
  console: false
  scrub: true
  instrument:
    pydantic_ai: false
    httpx: false
    sqlalchemy: false
```

`send_to_logfire: true` exports only when a `LOGFIRE_TOKEN` is present in the
environment. `scrub` redacts identifier attributes such as conversation addresses
and user, chat and message ids while keeping channel and run labels. `console`
prints spans to the terminal.

Octomate's own spans always emit whenever tracing is on: every kick, every reflex
node, every channel ingest, every workspace operation, every agent run. The three
`instrument` toggles are different. Each traces a whole library's internals, every
HTTP request or SQL statement or model round-trip, which is diagnostic volume rather
than always-on volume.

## Agent telemetry

Each harness-driven agent has an `instrument` flag in its tentacle block. On, the
agent's native spans are exported to the same Logfire project under the trace of the
kick that drove it, so a run reads as one tree from the channel message down to the
model calls.

| Agent | Setting | What arrives |
|---|---|---|
| Claude Code | `tentacles.<id>.instrument` | The CLI's native OTLP spans. Octomate exports the OTLP endpoint and headers into the process and the SDK propagates the active parent at launch. |
| Codex | `tentacles.<id>.instrument` | The app-server's native OTLP spans, configured through config overrides. The SDK has no public trace-context hook, so Octomate adds the W3C parent to each request envelope itself. |
| DeepSeek Harness | `tentacles.<id>.instrument` | No native trace exporter exists. Octomate records each session event as a log record under the kick instead. |
| Inkling | `logfire.instrument.pydantic_ai` | Pydantic AI's own instrumentation, which already inherits the active span. |

Native sessions you run yourself are not affected by these flags. Their transcripts
are recorded through the hooks and the tail; nothing is injected into your own
process.
