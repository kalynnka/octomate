"""Feelers — per-tentacle interactive API, split into composable parts.

Each sub-feeler is an independent ABC covering one interaction domain.
Platform subclasses implement the abstract send / render methods.
Persistence (InteractionStore) is not wired up in this iteration — concrete
implementations and null variants land alongside it later.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from octomate.schemas.actions import (
    ConfirmAction,
    QuestionAction,
    QuestionResponse,
    TodoAction,
)
from octomate.schemas.conversation import ConversationKey


class ConfirmationFeeler(ABC):
    """Handles HITL confirmation cards."""

    @abstractmethod
    async def create_confirmation(
        self,
        key: ConversationKey,
        tool_name: str,
        tool_call_id: str,
        args: dict[str, Any],
        title: str = "",
        description: str = "",
        skill: str = "",
        approvers: list[str] | None = None,
    ) -> tuple[ConfirmAction, asyncio.Future[bool]]: ...

    @abstractmethod
    async def send_confirmation(
        self, key: ConversationKey, action: ConfirmAction
    ) -> bool: ...

    @abstractmethod
    async def send_timeout_notification(
        self, key: ConversationKey, action: ConfirmAction
    ) -> None: ...

    @abstractmethod
    async def dismiss_confirmation(
        self, key: ConversationKey, action: ConfirmAction
    ) -> None: ...


class QuestionFeeler(ABC):
    """Handles question / input-collection cards."""

    @abstractmethod
    async def create_question(
        self,
        key: ConversationKey,
        text: str,
        options: list[str] | None = None,
    ) -> tuple[QuestionAction, asyncio.Future[QuestionResponse]]: ...

    @abstractmethod
    async def ask_question(
        self,
        key: ConversationKey,
        text: str,
        conversation_key: ConversationKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> QuestionResponse | None: ...

    @abstractmethod
    async def dismiss_question(
        self, key: ConversationKey, question: QuestionAction
    ) -> None: ...


class TodoFeeler(ABC):
    """Handles TODO list cards."""

    @abstractmethod
    async def create_todo(
        self,
        key: ConversationKey,
        title: str,
        active_form: str | None = None,
        assignee: str | None = None,
    ) -> TodoAction: ...

    @abstractmethod
    async def update_todo(self, todo_id: str, status: str) -> bool: ...

    @abstractmethod
    async def upsert_todo_list(
        self,
        key: ConversationKey,
        items: list[TodoAction],
        existing_ts: str | None = None,
    ) -> str | None: ...

    @abstractmethod
    async def pin_todo(self, key: ConversationKey, card_ref: str) -> bool: ...

    @abstractmethod
    async def unpin_todo(self, key: ConversationKey, card_ref: str) -> bool: ...


@dataclass
class Feelers:
    """Composed collection of per-tentacle interactive feeler parts."""

    confirm: ConfirmationFeeler
    todos: TodoFeeler
    questions: QuestionFeeler
