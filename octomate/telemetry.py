from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

import logfire
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
