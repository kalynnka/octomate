from __future__ import annotations

import asyncio
import time
import uuid

from octomate.schemas.actions import ConfirmAction
from octomate.schemas.session import SessionKey


class ConfirmationStore:
    pending: dict[str, tuple[ConfirmAction, asyncio.Future[bool]]]
    timeout: float

    def __init__(self, timeout: float = 60.0) -> None:
        self.pending = {}
        self.timeout = timeout

    def create(
        self,
        session_key: SessionKey,
        tool_name: str,
        tool_call_id: str,
        args: dict,
        description: str = "",
        approvers: list[str] | None = None,
    ) -> tuple[ConfirmAction, asyncio.Future[bool]]:
        confirmation_id = uuid.uuid4().hex
        now = time.time()
        action = ConfirmAction(
            confirmation_id=confirmation_id,
            session_key=session_key,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=args,
            description=description,
            approvers=approvers or [],
            created_at=now,
            expires_at=now + self.timeout,
        )
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self.pending[confirmation_id] = (action, future)
        return action, future

    def resolve(self, confirmation_id: str, approved: bool) -> bool:
        entry = self.pending.pop(confirmation_id, None)
        if entry is None:
            return False
        action, future = entry
        if future.done():
            return False
        action.status = "approved" if approved else "denied"
        future.set_result(approved)
        return True

    def expire(self, confirmation_id: str) -> None:
        entry = self.pending.pop(confirmation_id, None)
        if entry is None:
            return
        action, future = entry
        action.status = "expired"
        if not future.done():
            future.set_result(False)
