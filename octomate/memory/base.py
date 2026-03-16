from __future__ import annotations

import logging
import pickle
from collections import defaultdict, deque
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.session import SessionKey

logger = logging.getLogger(__name__)


class OctopusMemory:
    message_store: dict[SessionKey, deque[list[ModelMessage]]]
    max_messages: int
    store_path: Path

    def __init__(
        self,
        max_messages: int = 32,
        store_path: Path = Path(".octomate/message_store"),
    ) -> None:
        self.max_messages = max_messages
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
        self.store_path.write_bytes(pickle.dumps(dict(self.message_store)))
        logger.info("Saved message store to %s", self.store_path)

    def record(self, key: SessionKey, messages: list[ModelMessage]) -> None:
        self.message_store[key].append(messages)

    def history(self, key: SessionKey) -> list[ModelMessage]:
        return [msg for batch in self.message_store[key] for msg in batch]

    async def recall(self, key: SessionKey, query: str, limit: int = 5) -> list[str]:
        return []

    async def memo(self, key: SessionKey, messages: list[ModelMessage]) -> None:
        pass

    @staticmethod
    def messages_to_dicts(
        messages: list[ModelMessage],
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for msg in messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, UserPromptPart) and isinstance(
                        part.content, str
                    ):
                        result.append({"role": "user", "content": part.content})
            elif isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, TextPart):
                        result.append({"role": "assistant", "content": part.content})
        return result
