from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from octomate.types.json import JsonObject


class RolloutLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: datetime
    type: Literal[
        "session_meta", "turn_context", "event_msg", "response_item", "world_state"
    ]
    payload: JsonObject


rollout_line_adapter = TypeAdapter(RolloutLine)


def payload_type(line: RolloutLine) -> str | None:
    value = line.payload.get("type")
    return value if isinstance(value, str) else None
