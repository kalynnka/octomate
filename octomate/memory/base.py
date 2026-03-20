from __future__ import annotations

import dataclasses
import logging
import pickle
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.actions import AgentMessage
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)


class OctopusMemory:
    message_store: dict[SessionKey, deque[list[ModelMessage]]]
    max_messages: int
    history_size: int
    store_path: Path

    def __init__(
        self,
        max_messages: int = 32,
        history_size: int = 16,
        store_path: Path = Path(".octomate/message_store"),
    ) -> None:
        self.max_messages = max_messages
        self.history_size = min(history_size, max_messages)
        self.store_path = store_path
        self.message_store = self.load()

    def load(self) -> dict[SessionKey, deque[list[ModelMessage]]]:
        store: dict[SessionKey, deque[list[ModelMessage]]] = defaultdict(
            lambda: deque(maxlen=self.max_messages)
        )
        if self.store_path.exists():
            try:
                store.update(pickle.loads(self.store_path.read_bytes()))
            except Exception:
                logger.warning(
                    "Failed to load message store, starting fresh", exc_info=True
                )
        logger.info("Loaded message store from %s", self.store_path)
        return store

    def save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(dict(self.message_store)))
        tmp.replace(self.store_path)
        logger.info("Saved message store to %s", self.store_path)

    def record(self, key: SessionKey, messages: list[ModelMessage]) -> None:
        filtered: list[ModelMessage] = []
        for msg in messages:
            if isinstance(msg, ModelRequest):
                parts = [p for p in msg.parts if isinstance(p, UserPromptPart)]
                if parts:
                    filtered.append(dataclasses.replace(msg, parts=parts))
            elif isinstance(msg, ModelResponse):
                parts = [p for p in msg.parts if isinstance(p, TextPart)]
                if parts:
                    filtered.append(dataclasses.replace(msg, parts=parts))
        if filtered:
            self.message_store[key].append(filtered)

    def history(self, key: SessionKey, size: int | None = None) -> list[ModelMessage]:
        batches = self.message_store[key]
        n = min(size or self.history_size, self.max_messages)
        recent = list(batches)[-n:]
        return [msg for batch in recent for msg in batch]

    async def recall(
        self,
        key: SessionKey,
        events: list[MessageEvent],
        tentacle: Tentacle,
        limit: int = 5,
    ) -> list[str]:
        """Persist user messages via memo, then retrieve relevant memories.

        Calls ``memo`` with the incoming *events* first so that user messages
        are recorded before the retrieval step.
        """
        await self.memo(key, events, tentacle)
        return []

    async def memo(
        self,
        key: SessionKey,
        messages: list[AgentMessage] | list[MessageEvent],
        tentacle: Tentacle,
    ) -> None:
        pass
