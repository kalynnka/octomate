from __future__ import annotations

from openai_codex.client import CodexClient
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from octomate.types.json import JsonObject


class TracedCodexClient(CodexClient):
    """Propagate the current kick on each request, including reused SDK processes."""

    def _write_message(self, payload: JsonObject) -> None:
        # openai-codex 0.147.0 has no public trace-context argument or envelope hook.
        # Keep the SDK transport and add the app-server's top-level W3C `trace`.
        # Replace this override when the SDK exposes propagation; see
        # docs/agent-telemetry.md for the checked versions and native export test.
        if "id" in payload and "method" in payload:
            carrier: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(carrier)
            if carrier:
                # The comprehension widens values to JsonValue; dict(carrier) does not.
                context: JsonObject = {key: value for key, value in carrier.items()}  # noqa: C416
                payload = {**payload, "trace": context}
        super()._write_message(payload)
