from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from octomate.schemas.actions import ConfirmAction
from octomate.schemas.session import SessionKey


@dataclass
class TodoItem:
    todo_id: str
    title: str
    status: str = "pending"  # pending | in_progress | completed | cancelled
    active_form: str | None = None
    assignee: str | None = None


@dataclass
class Question:
    question_id: str
    text: str
    options: list[str] | None = None  # None = free-text input


@dataclass
class QuestionResponse:
    question_id: str
    answer: str
    responder_id: str


class InteractionStore:
    """Per-tentacle store for pending confirmations, questions, and TODOs."""

    confirmations: dict[str, tuple[ConfirmAction, asyncio.Future[bool]]]
    questions: dict[str, tuple[Question, asyncio.Future[QuestionResponse]]]
    todos: dict[str, TodoItem]
    timeout: float

    def __init__(self, timeout: float = 60.0) -> None:
        self.confirmations = {}
        self.questions = {}
        self.todos = {}
        self.timeout = timeout

    # --- Confirmations ---

    def create_confirmation(
        self,
        session_key: SessionKey,
        tool_name: str,
        tool_call_id: str,
        args: dict,
        title: str = "",
        description: str = "",
        skill: str = "",
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
            title=title,
            description=description,
            skill=skill,
            approvers=approvers or [],
            created_at=now,
            expires_at=now + self.timeout,
        )
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.confirmations[confirmation_id] = (action, future)
        return action, future

    def resolve_confirmation(self, confirmation_id: str, approved: bool) -> bool:
        entry = self.confirmations.pop(confirmation_id, None)
        if entry is None:
            return False
        action, future = entry
        if future.done():
            return False
        action.status = "approved" if approved else "denied"
        future.set_result(approved)
        return True

    def expire_confirmation(self, confirmation_id: str) -> None:
        entry = self.confirmations.pop(confirmation_id, None)
        if entry is None:
            return
        action, future = entry
        action.status = "expired"
        if not future.done():
            future.set_result(False)

    # --- Questions ---

    def create_question(
        self, text: str, options: list[str] | None = None
    ) -> tuple[Question, asyncio.Future[QuestionResponse]]:
        question_id = uuid.uuid4().hex
        question = Question(question_id=question_id, text=text, options=options)
        future: asyncio.Future[QuestionResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.questions[question_id] = (question, future)
        return question, future

    def resolve_question(
        self, question_id: str, answer: str, responder_id: str
    ) -> bool:
        entry = self.questions.pop(question_id, None)
        if entry is None:
            return False
        _, future = entry
        if future.done():
            return False
        future.set_result(
            QuestionResponse(
                question_id=question_id, answer=answer, responder_id=responder_id
            )
        )
        return True

    def expire_question(self, question_id: str) -> None:
        entry = self.questions.pop(question_id, None)
        if entry is None:
            return
        _, future = entry
        if not future.done():
            future.cancel()

    # --- TODOs ---

    def create_todo(
        self, title: str, active_form: str | None = None, assignee: str | None = None
    ) -> TodoItem:
        todo_id = uuid.uuid4().hex
        item = TodoItem(
            todo_id=todo_id, title=title, active_form=active_form, assignee=assignee
        )
        self.todos[todo_id] = item
        return item

    def update_todo(self, todo_id: str, status: str) -> bool:
        item = self.todos.get(todo_id)
        if item is None:
            return False
        item.status = status
        return True
