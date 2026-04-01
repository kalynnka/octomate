"""Feelers — per-tentacle interactive API, split into composable parts.

Each sub-feeler is an independent ABC covering one interaction domain.
Platform subclasses implement the abstract *send* / *render* methods.
Persistence is delegated to InteractionStore via self.store.

Assemble them into a Feelers container to inject into a Tentacle.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from octomate.stores.interaction import InteractionStore, QuestionResponse
from octomate.transmuters.interactions import Confirmation, Question, Todo

if TYPE_CHECKING:
    from octomate.schemas.session import SessionKey


class ConfirmationFeeler(ABC):
    """Handles HITL confirmation cards. Delegates persistence to store."""

    store: InteractionStore

    def __init__(self, store: InteractionStore) -> None:
        self.store = store

    @property
    def timeout(self) -> float:
        return self.store.timeout

    def is_session_allowed(self, thread_id: str, tool_name: str) -> bool:
        return self.store.is_session_allowed(thread_id, tool_name)

    def get_confirmation(
        self, confirmation_id: str
    ) -> tuple[Confirmation, asyncio.Future[bool]] | None:
        return self.store.confirmations.get(confirmation_id)

    async def create_confirmation(
        self,
        key: SessionKey,
        tool_name: str,
        tool_call_id: str,
        args: dict,
        title: str = "",
        description: str = "",
        skill: str = "",
        approvers: list[str] | None = None,
    ) -> tuple[Confirmation, asyncio.Future[bool]]:
        return await self.store.create_confirmation(
            key=key,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=args,
            title=title,
            description=description,
            skill=skill,
            approvers=approvers,
        )

    async def resolve_confirmation(self, confirmation_id: str, approved: bool) -> bool:
        return await self.store.resolve_confirmation(confirmation_id, approved)

    async def expire_confirmation(self, confirmation_id: str) -> None:
        await self.store.expire_confirmation(confirmation_id)

    async def resolve_confirmation_allow_session(self, confirmation_id: str) -> bool:
        return await self.store.resolve_confirmation_allow_session(confirmation_id)

    @abstractmethod
    async def send_confirmation(self, key: SessionKey, action: Confirmation) -> bool:
        """Send an approval card. Returns True if delivered."""
        ...

    @abstractmethod
    async def send_timeout_notification(
        self, key: SessionKey, action: Confirmation
    ) -> None:
        """Notify the channel that a tool call was auto-refused due to timeout/kill."""
        ...

    @abstractmethod
    async def dismiss_confirmation(
        self, key: SessionKey, action: Confirmation
    ) -> None:
        """Update the platform card to show a cancelled/dismissed state."""
        ...


class TodoFeeler(ABC):
    """Handles TODO list cards. Delegates persistence to store."""

    store: InteractionStore

    def __init__(self, store: InteractionStore) -> None:
        self.store = store

    async def create_todo(
        self,
        key: SessionKey,
        title: str,
        active_form: str | None = None,
        assignee: str | None = None,
    ) -> Todo:
        return await self.store.create_todo(
            key=key, title=title, active_form=active_form, assignee=assignee
        )

    async def update_todo(self, todo_id: str, status: str) -> bool:
        return await self.store.update_todo(todo_id, status)

    @abstractmethod
    async def upsert_todo_list(
        self,
        key: SessionKey,
        items: list[Todo],
        existing_ts: str | None = None,
    ) -> str | None:
        """Create or update a single consolidated TODO list card."""
        ...

    @abstractmethod
    async def pin_todo(self, key: SessionKey, card_ref: str) -> bool:
        """Pin the todo list message. Unpins any previously pinned list first."""
        ...

    @abstractmethod
    async def unpin_todo(self, key: SessionKey, card_ref: str) -> bool:
        """Unpin the todo list message."""
        ...


class QuestionFeeler(ABC):
    """Handles question / input-collection cards. Delegates persistence to store."""

    store: InteractionStore

    def __init__(self, store: InteractionStore) -> None:
        self.store = store

    @property
    def timeout(self) -> float:
        return self.store.timeout

    def get_question(
        self, question_id: str
    ) -> tuple[Question, asyncio.Future[QuestionResponse]] | None:
        return self.store.questions.get(question_id)

    async def create_question(
        self,
        key: SessionKey,
        text: str,
        options: list[str] | None = None,
    ) -> tuple[Question, asyncio.Future[QuestionResponse]]:
        return await self.store.create_question(key, text, options)

    async def resolve_question(
        self, question_id: str, answer: str, responder_id: str
    ) -> bool:
        return await self.store.resolve_question(question_id, answer, responder_id)

    async def expire_question(self, question_id: str) -> None:
        await self.store.expire_question(question_id)

    @abstractmethod
    async def ask_question(
        self,
        key: SessionKey,
        text: str,
        session_key: SessionKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> QuestionResponse | None:
        """Send a question card and wait for a response."""
        ...

    @abstractmethod
    async def dismiss_question(self, key: SessionKey, question: Question) -> None:
        """Update the platform card to show a cancelled/dismissed state."""
        ...

    async def collect_input(
        self, key: SessionKey, prompt: str, session_key: SessionKey
    ) -> str | None:
        resp = await self.ask_question(key, prompt, session_key)
        return resp.answer if resp else None


@dataclass
class Feelers:
    """Composed collection of per-tentacle interactive feeler parts."""

    confirm: ConfirmationFeeler
    todos: TodoFeeler
    questions: QuestionFeeler


# --- Null implementations (no-op for platforms without card support) ---


class NullConfirmationFeeler(ConfirmationFeeler):
    def __init__(self) -> None:
        pass

    def is_session_allowed(self, thread_id: str, tool_name: str) -> bool:
        return False

    def get_confirmation(self, confirmation_id: str) -> None:
        return None

    async def create_confirmation(
        self, **kwargs
    ) -> tuple[Confirmation, asyncio.Future[bool]]:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        future.set_result(False)
        return Confirmation(
            confirmation_id="",
            thread_id="",
            tool_name="",
            tool_call_id="",
            args={},
            expires_at=datetime.now(),
        ), future

    async def resolve_confirmation(self, confirmation_id: str, approved: bool) -> bool:
        return False

    async def expire_confirmation(self, confirmation_id: str) -> None:
        pass

    async def resolve_confirmation_allow_session(self, confirmation_id: str) -> bool:
        return False

    async def send_confirmation(self, key: SessionKey, action: Confirmation) -> bool:
        _ = key, action
        return False

    async def send_timeout_notification(
        self, key: SessionKey, action: Confirmation
    ) -> None:
        _ = key, action

    async def dismiss_confirmation(
        self, key: SessionKey, action: Confirmation
    ) -> None:
        _ = key, action


class NullTodoFeeler(TodoFeeler):
    def __init__(self) -> None:
        pass

    async def create_todo(self, key: SessionKey, title: str, **kwargs) -> Todo:
        return Todo(todo_id="", title=title)

    async def update_todo(self, todo_id: str, status: str) -> bool:
        return False

    async def upsert_todo_list(
        self,
        key: SessionKey,
        items: list[Todo],
        existing_ts: str | None = None,
    ) -> None:
        _ = key, items, existing_ts
        return None

    async def pin_todo(self, key: SessionKey, card_ref: str) -> bool:
        _ = key, card_ref
        return False

    async def unpin_todo(self, key: SessionKey, card_ref: str) -> bool:
        _ = key, card_ref
        return False


class NullQuestionFeeler(QuestionFeeler):
    def __init__(self) -> None:
        pass

    def get_question(self, question_id: str) -> None:
        return None

    async def create_question(
        self, key: SessionKey, text: str, options: list[str] | None = None
    ):
        future: asyncio.Future[QuestionResponse] = (
            asyncio.get_running_loop().create_future()
        )
        future.cancel()
        return Question(
            question_id="", thread_id="", text=text, options=options
        ), future

    async def resolve_question(
        self, question_id: str, answer: str, responder_id: str
    ) -> bool:
        return False

    async def expire_question(self, question_id: str) -> None:
        pass

    async def ask_question(
        self,
        key: SessionKey,
        text: str,
        session_key: SessionKey,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> None:
        _ = key, text, session_key, options, multi_select
        return None

    async def dismiss_question(self, key: SessionKey, question: Question) -> None:
        _ = key, question


NULL_FEELERS = Feelers(
    confirm=NullConfirmationFeeler(),
    todos=NullTodoFeeler(),
    questions=NullQuestionFeeler(),
)
