from __future__ import annotations

import uuid
from typing import TypeAlias

from pydantic import BaseModel
from octomate.schemas.events import MessageEvent


UserMessages: TypeAlias = list[MessageEvent]


class DeferredActionResponse(BaseModel):
    action_id: uuid.UUID
    responder_id: str = ""
    answer: str | None = None
    approved: bool | None = None
    allow_session: bool = False


AwakeSignal: TypeAlias = UserMessages | DeferredActionResponse
