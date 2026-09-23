"""The native hook fields consumed by the forwarding and launcher clients."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class NativeHookEvent(BaseModel):
    """Validate inspected fields while carrying other runtime fields unchanged."""

    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    hook_event_name: str
    session_id: str
    transcript_path: str | None = None
    cwd: str = ""
    agent_id: str | None = None
    agent_transcript_path: str | None = None
    session_name: str | None = Field(
        default=None,
        description="Session title read by the hook client through the runtime SDK.",
    )
