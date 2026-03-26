"""Feelers — per-tentacle interactive API, split into composable parts.

Each sub-feeler is an independent ABC covering one interaction domain.
Assemble them into a Feelers container to inject into a Tentacle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octomate.schemas.session import SessionKey
    from octomate.stores import QuestionResponse
    from octomate.tentacles.base import SendTarget
    from octomate.transmuters.interactions import Confirmation, Todo


class ConfirmationFeeler(ABC):
    """Handles HITL confirmation cards."""

    @abstractmethod
    async def send_confirmation(self, target: SendTarget, action: Confirmation) -> bool:
        """Send an approval card. Returns True if delivered."""
        ...

    @abstractmethod
    async def send_timeout_notification(self, target: SendTarget, action: Confirmation) -> None:
        """Notify the channel that a tool call was auto-refused due to timeout/kill."""
        ...


class TodoFeeler(ABC):
    """Handles TODO list cards (single consolidated card, reference-only)."""

    @abstractmethod
    async def upsert_todo_list(
        self,
        target: SendTarget,
        items: list[Todo],
        existing_ts: str | None = None,
    ) -> str | None:
        """Create or update a single consolidated TODO list card.

        Pass existing_ts=None to post a new card; pass the previously returned
        ts/message-id to update it in-place.  Returns the ts/message-id on
        success, or None on failure.
        """
        ...

    @abstractmethod
    async def pin_todo(self, target: SendTarget, ts: str) -> bool:
        """Pin the todo list message. Unpins any previously pinned list first."""
        ...

    @abstractmethod
    async def unpin_todo(self, target: SendTarget, ts: str) -> bool:
        """Unpin the todo list message."""
        ...


class QuestionFeeler(ABC):
    """Handles question and input-collection cards."""

    @abstractmethod
    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        session_key: SessionKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> QuestionResponse | None:
        """Send a question card and wait for a response.

        options=None requests free-text input (platform-dependent).
        Returns None on timeout or unsupported mode.
        """
        ...

    async def collect_input(
        self, target: SendTarget, prompt: str, session_key: SessionKey
    ) -> str | None:
        """Convenience wrapper: ask a free-text question, return the raw answer."""
        resp = await self.ask_question(target, prompt, session_key)
        return resp.answer if resp else None


@dataclass
class Feelers:
    """Composed collection of per-tentacle interactive feeler parts."""

    confirm: ConfirmationFeeler
    todos: TodoFeeler
    questions: QuestionFeeler


# --- Null implementations (no-op for platforms without card support) ---


class NullConfirmationFeeler(ConfirmationFeeler):
    async def send_confirmation(self, target: SendTarget, action: Confirmation) -> bool:
        _ = target, action
        return False

    async def send_timeout_notification(self, target: SendTarget, action: Confirmation) -> None:
        _ = target, action


class NullTodoFeeler(TodoFeeler):
    async def upsert_todo_list(
        self,
        target: SendTarget,
        items: list[Todo],
        existing_ts: str | None = None,
    ) -> None:
        _ = target, items, existing_ts
        return None

    async def pin_todo(self, target: SendTarget, ts: str) -> bool:
        _ = target, ts
        return False

    async def unpin_todo(self, target: SendTarget, ts: str) -> bool:
        _ = target, ts
        return False


class NullQuestionFeeler(QuestionFeeler):
    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        session_key: SessionKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> None:
        _ = target, text, session_key, options, multi_select
        return None


NULL_FEELERS = Feelers(
    confirm=NullConfirmationFeeler(),
    todos=NullTodoFeeler(),
    questions=NullQuestionFeeler(),
)
