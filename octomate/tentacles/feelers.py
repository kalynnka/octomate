"""Feelers — per-tentacle interactive API, split into composable parts.

Each sub-feeler is an independent ABC covering one interaction domain.
Assemble them into a Feelers container to inject into a Tentacle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octomate.schemas.actions import ConfirmAction
    from octomate.store import QuestionResponse, TodoItem
    from octomate.tentacles.base import SendTarget


class ConfirmationFeeler(ABC):
    """Handles HITL confirmation cards."""

    @abstractmethod
    async def send_confirmation(self, target: SendTarget, action: ConfirmAction) -> bool:
        """Send an approval card. Returns True if delivered."""
        ...


class TodoFeeler(ABC):
    """Handles TODO list cards."""

    @abstractmethod
    async def create_todo(
        self, target: SendTarget, title: str, assignee: str | None = None
    ) -> TodoItem | None:
        """Create and send a TODO card. Returns the item, or None on failure."""
        ...

    @abstractmethod
    async def update_todo(self, todo_id: str, status: str) -> bool: ...


class QuestionFeeler(ABC):
    """Handles question and input-collection cards."""

    @abstractmethod
    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        options: list[str] | None = None,
    ) -> QuestionResponse | None:
        """Send a question card and wait for a response.

        options=None requests free-text input (platform-dependent).
        Returns None on timeout or unsupported mode.
        """
        ...

    async def collect_input(self, target: SendTarget, prompt: str) -> str | None:
        """Convenience wrapper: ask a free-text question, return the raw answer."""
        resp = await self.ask_question(target, prompt)
        return resp.answer if resp else None


@dataclass
class Feelers:
    """Composed collection of per-tentacle interactive feeler parts."""

    confirm: ConfirmationFeeler
    todos: TodoFeeler
    questions: QuestionFeeler


# --- Null implementations (no-op for platforms without card support) ---


class NullConfirmationFeeler(ConfirmationFeeler):
    async def send_confirmation(self, target: SendTarget, action: ConfirmAction) -> bool:
        _ = target, action
        return False


class NullTodoFeeler(TodoFeeler):
    async def create_todo(
        self, target: SendTarget, title: str, assignee: str | None = None
    ) -> None:
        _ = target, title, assignee
        return None

    async def update_todo(self, todo_id: str, status: str) -> bool:
        _ = todo_id, status
        return False


class NullQuestionFeeler(QuestionFeeler):
    async def ask_question(
        self,
        target: SendTarget,
        text: str,
        options: list[str] | None = None,
    ) -> None:
        _ = target, text, options
        return None


NULL_FEELERS = Feelers(
    confirm=NullConfirmationFeeler(),
    todos=NullTodoFeeler(),
    questions=NullQuestionFeeler(),
)
