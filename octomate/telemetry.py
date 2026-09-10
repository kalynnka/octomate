from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import quote

import logfire
from pydantic import SecretStr
from pydantic_ai import InstrumentationSettings
from pydantic_ai.messages import ModelMessage, ModelRequest, UserContent, UserPromptPart
from pydantic_core import to_json

# One Logfire instance per subsystem, each pinning its own otel scope (`logfire.claude`,
# `logfire.slack`, …) instead of the default `logfire` every call site would otherwise
# share. The scope is what makes a trace filterable by who emitted it — spans from a
# tentacle, the host, and the reflex graph all land in one stream, and only the scope
# tells them apart without matching on span names.
#
# They live here, not next to their call sites, so one subsystem means one scope and one
# instance: the same name declared in three modules would be three instances of the same
# thing, and three places to keep in step. Deriving an instance is cheap and does not
# read config — `logfire.configure()` mutates the config these share by reference, so
# importing this module before the app configures logfire is fine.

octomate_logfire = logfire.with_settings(custom_scope_suffix="octomate")
reflex_logfire = logfire.with_settings(custom_scope_suffix="reflex")
deferred_logfire = logfire.with_settings(custom_scope_suffix="deferred")
react_logfire = logfire.with_settings(custom_scope_suffix="react")
workspace_logfire = logfire.with_settings(custom_scope_suffix="workspace")

# Channels: the shared plumbing every channel runs through, then the per-platform ones.
channel_logfire = logfire.with_settings(custom_scope_suffix="channel")
slack_logfire = logfire.with_settings(custom_scope_suffix="slack")
lark_logfire = logfire.with_settings(custom_scope_suffix="lark")

# Agents, each scoped to the runtime it fronts.
claude_logfire = logfire.with_settings(custom_scope_suffix="claude")
codex_logfire = logfire.with_settings(custom_scope_suffix="codex")
deepseek_logfire = logfire.with_settings(custom_scope_suffix="deepseek")
inkling_logfire = logfire.with_settings(custom_scope_suffix="inkling")


@dataclass(frozen=True)
class TraceEnvironment:
    endpoint: str  # Full OTLP/HTTP trace endpoint.
    token: SecretStr  # Logfire write token for native exporters.

    def as_env(self) -> dict[str, str]:
        return {
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": self.endpoint,
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "Authorization="
            + quote(self.token.get_secret_value(), safe=""),
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_EXPORTER": "otlp",
        }


def octomate_trace_environment() -> TraceEnvironment | None:
    """Give native exporters the same destination as the configured Logfire SDK."""
    config = octomate_logfire.config
    if not config.send_to_logfire or not config.token:
        return None
    if not isinstance(config.token, str):
        raise ValueError(
            "Native harness tracing requires a single Logfire project token"
        )
    return TraceEnvironment(
        endpoint=(
            config.advanced.generate_base_url(config.token).rstrip("/") + "/v1/traces"
        ),
        token=SecretStr(config.token),
    )


AgentInputMessageAttributes = TypedDict(
    "AgentInputMessageAttributes",
    {"gen_ai.input.messages": str, "logfire.json_schema": str},
)


def agent_input_message_attributes(
    user_prompt: str | Sequence[UserContent] | None,
) -> AgentInputMessageAttributes:
    """Render the current harness prompt like a Pydantic AI model request."""
    settings = InstrumentationSettings()
    messages: list[ModelMessage] = (
        [ModelRequest(parts=[UserPromptPart(content=user_prompt)])]
        if user_prompt
        else []
    )
    rendered = settings.messages_to_otel_messages(messages)
    return {
        "gen_ai.input.messages": to_json(rendered, fallback=str).decode(),
        "logfire.json_schema": to_json(
            {
                "type": "object",
                "properties": {"gen_ai.input.messages": {"type": "array"}},
            }
        ).decode(),
    }
