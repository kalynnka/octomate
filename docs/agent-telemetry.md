# Agent telemetry

Checked on 2026-09-08. Driven runs reuse the active kick's W3C trace context;
native exporters send to the endpoint and project token configured by Logfire.
Enable `agents.claude.instrument`, `agents.codex.instrument`, or
`agents.deepseek.instrument` individually. Inkling uses the existing
`logfire.instrument.pydantic_ai` setting.

| Agent | SDK / runtime checked | Export and parent support |
| --- | --- | --- |
| Codex | `openai-codex` and its bundled runtime `0.147.0` | Native OTLP spans retain each kick's parent through the request-envelope override described below. |
| Claude | `claude-agent-sdk 0.2.152`, bundled Claude Code `2.1.259` | Native OTLP spans; the SDK automatically propagates the active parent at process launch. |
| DeepSeek | Local dsh `0.1.0-rc.8`; published gateway and telemetry packages through `0.1.2-rc.1` and `0.1.3-alpha.2` | Native telemetry exports logs only; no trace exporter or inbound W3C parent context was found. Octomate records session events under the kick instead. |
| Inkling | `pydantic-ai 2.20.0`, harness capabilities `0.14.0` | Pydantic AI's public OpenTelemetry instrumentation retains the active parent in process. |

## SDK boundaries

**Codex:** PyPI has no newer stable or prerelease Python SDK than `0.147.0`.
`CodexConfig` configures the native exporter, but the public request API has no
trace-context argument or request-envelope hook. `TracedCodexClient` keeps the
SDK's transport and adds the app-server's top-level `trace` field in
`_write_message`. This private override needs checking on SDK upgrades and should
be removed when the SDK exposes propagation. A process environment cannot carry
a different parent for each kick when the client is reused. Startup and background
spans outside a kick can still have independent trace IDs.

**Claude:** Upgraded from `0.1.80` after testing the newer SDK. Connect inside the
driving span: the SDK injects `TRACEPARENT` and `TRACESTATE` into the CLI environment.
Each Octomate run opens a client, including resumed sessions. Native tracing remains
beta. Logfire's alternative `instrument_claude_agent_sdk()` still uses thread-local
conversation state in `5.0.0`; native export avoids sharing that state between
concurrent asyncio runs without upgrading Logfire.

**DeepSeek:** Updating the gateway or telemetry package does not supply native
parented spans. Its OTel backend uses `LoggerProvider` / `OTLPLogExporter`, and the
gateway has no W3C context input. The current event records omit token chunks and
are emitted from the driving consumer, whose context belongs to the kick; they are
not native model or tool spans. Revisit when dsh provides both spans and propagation.

**Inkling:** Native parenting already works through the installed Pydantic AI
instrumentation. Newer Pydantic AI and harness releases exist, but no upgrade is
needed for this trace support.

## Verification

Local native-runtime smoke tests used temporary homes, synthetic credentials, mock
model endpoints, and an OTLP receiver. Two Codex turns on one SDK client exported
`turn/start` with their respective parent span IDs and model spans in the matching
trace. Claude exported an interaction and child model-request span for each of two
kicks, including resuming the session under the second parent. Both native smoke
tests passed. The selected agent, configuration, and telemetry suite passed
428 tests; it also captures DeepSeek event records and Inkling model spans to check
their trace context. Ruff and Pyright basic checks passed. The full repository
suite and live Logfire project delivery were not checked.

Sources: [official OpenAI SDK documentation](https://learn.chatgpt.com/docs/codex-sdk),
[Codex exporter configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[Claude SDK observability](https://code.claude.com/docs/en/agent-sdk/observability),
[Pydantic AI instrumentation](https://pydantic.dev/docs/ai/integrations/logfire/),
and the published Python wheels and DeepSeek npm gateway/telemetry packages.
