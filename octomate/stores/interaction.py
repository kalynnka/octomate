from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from arcanus.materia.sqlalchemy import AsyncSession
from sqlalchemy import update
from uuid_utils import uuid7

from octomate.database import engine
from octomate.models.interactions import (
    Confirmation as ConfirmationModel,
)
from octomate.models.interactions import (
    Question as QuestionModel,
)
from octomate.models.interactions import (
    Todo as TodoModel,
)
from octomate.schemas.session import SessionKey
from octomate.stores.thread import ThreadStore
from octomate.transmuters.interactions import Confirmation, Question, Todo


@dataclass
class QuestionResponse:
    question_id: str
    answer: str
    responder_id: str


class InteractionStore:
    """Per-tentacle store for pending confirmations, questions, and TODOs."""

    confirmations: dict[str, tuple[Confirmation, asyncio.Future[bool]]]
    questions: dict[str, tuple[Question, asyncio.Future[QuestionResponse]]]
    todos: dict[str, Todo]
    timeout: float
    threads: ThreadStore

    card_message_ids: dict[str, str]  # interaction_id -> platform message id

    def __init__(self, threads: ThreadStore, timeout: float = 3600.0) -> None:
        self.confirmations = {}
        self.questions = {}
        self.todos = {}
        self.card_message_ids = {}
        self.timeout = timeout
        self.threads = threads
        self.session_allowlist: dict[str, set[str]] = {}  # thread_id -> set[tool_name]

    def set_card_message_id(self, interaction_id: str, message_id: str) -> None:
        self.card_message_ids[interaction_id] = message_id

    def is_session_allowed(self, thread_id: str, tool_name: str) -> bool:
        return tool_name in self.session_allowlist.get(thread_id, set())

    def allow_in_session(self, thread_id: str, tool_name: str) -> None:
        self.session_allowlist.setdefault(thread_id, set()).add(tool_name)

    # --- Confirmations ---

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
        confirmation_id = uuid7().hex
        now = datetime.now()
        thread = await self.threads.get(key)

        async with AsyncSession(engine()) as session:
            confirmation = Confirmation(
                confirmation_id=confirmation_id,
                thread_id=str(thread.id),
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args=args,
                title=title,
                description=description,
                skill=skill,
                approvers=approvers or [],
                created_at=now,
                expires_at=datetime.fromtimestamp(now.timestamp() + self.timeout),
            )
            session.add(confirmation)
            await session.commit()

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.confirmations[confirmation_id] = (confirmation, future)
        return confirmation, future

    async def resolve_confirmation(self, confirmation_id: str, approved: bool) -> bool:
        entry = self.confirmations.pop(confirmation_id, None)
        if entry is None:
            return False
        confirmation, future = entry
        if future.done():
            return False
        status = "approved" if approved else "denied"
        future.set_result(approved)

        async with AsyncSession(engine()) as session:
            await session.execute(
                update(ConfirmationModel)
                .where(ConfirmationModel.confirmation_id == confirmation_id)
                .values(status=status)
            )
            await session.commit()

        return True

    async def expire_confirmation(self, confirmation_id: str) -> None:
        entry = self.confirmations.pop(confirmation_id, None)
        if entry is None:
            return
        _, future = entry
        if not future.done():
            future.set_result(False)

        async with AsyncSession(engine()) as session:
            await session.execute(
                update(ConfirmationModel)
                .where(ConfirmationModel.confirmation_id == confirmation_id)
                .values(status="expired")
            )
            await session.commit()

    async def resolve_confirmation_allow_session(self, confirmation_id: str) -> bool:
        entry = self.confirmations.pop(confirmation_id, None)
        if entry is None:
            return False
        confirmation, future = entry
        if future.done():
            return False
        self.allow_in_session(confirmation.thread_id, confirmation.tool_name)
        future.set_result(True)

        async with AsyncSession(engine()) as session:
            await session.execute(
                update(ConfirmationModel)
                .where(ConfirmationModel.confirmation_id == confirmation_id)
                .values(status="approved")
            )
            await session.commit()

        return True

    # --- Questions ---

    async def create_question(
        self,
        key: SessionKey,
        text: str,
        options: list[str] | None = None,
    ) -> tuple[Question, asyncio.Future[QuestionResponse]]:
        question_id = uuid7().hex
        thread = await self.threads.get(key)

        async with AsyncSession(engine()) as session:
            question = Question(
                question_id=question_id,
                thread_id=str(thread.id),
                text=text,
                options=options,
            )
            session.add(question)
            await session.commit()

        future: asyncio.Future[QuestionResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.questions[question_id] = (question, future)
        return question, future

    async def resolve_question(
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

        async with AsyncSession(engine()) as session:
            await session.execute(
                update(QuestionModel)
                .where(QuestionModel.question_id == question_id)
                .values(status="answered", answer=answer, responder_id=responder_id)
            )
            await session.commit()

        return True

    async def expire_question(self, question_id: str) -> None:
        entry = self.questions.pop(question_id, None)
        if entry is None:
            return
        _, future = entry
        if not future.done():
            future.cancel()

        async with AsyncSession(engine()) as session:
            await session.execute(
                update(QuestionModel)
                .where(QuestionModel.question_id == question_id)
                .values(status="expired")
            )
            await session.commit()

    async def expire_thread_interactions(
        self, thread_id: str
    ) -> tuple[list[Confirmation], list[Question]]:
        """Expire all pending confirmations and questions for a thread.

        Returns the expired (Confirmation, Question) lists so callers can
        dismiss the platform cards.
        """
        conf_ids = [
            cid
            for cid, (c, _) in list(self.confirmations.items())
            if c.thread_id == thread_id
        ]
        q_ids = [
            qid
            for qid, (q, _) in list(self.questions.items())
            if q.thread_id == thread_id
        ]
        expired_confirmations: list[Confirmation] = []
        expired_questions: list[Question] = []
        for cid in conf_ids:
            entry = self.confirmations.pop(cid, None)
            if entry is None:
                continue
            confirmation, future = entry
            expired_confirmations.append(confirmation)
            if not future.done():
                future.set_result(False)
        for qid in q_ids:
            entry = self.questions.pop(qid, None)
            if entry is None:
                continue
            question, future = entry
            expired_questions.append(question)
            if not future.done():
                future.cancel()

        async with AsyncSession(engine()) as session:
            if conf_ids:
                await session.execute(
                    update(ConfirmationModel)
                    .where(ConfirmationModel.confirmation_id.in_(conf_ids))
                    .values(status="expired")
                )
            if q_ids:
                await session.execute(
                    update(QuestionModel)
                    .where(QuestionModel.question_id.in_(q_ids))
                    .values(status="expired")
                )
            if conf_ids or q_ids:
                await session.commit()

        return expired_confirmations, expired_questions

    # --- TODOs ---

    async def create_todo(
        self,
        key: SessionKey,
        title: str,
        active_form: str | None = None,
        assignee: str | None = None,
    ) -> Todo:
        todo_id = uuid7().hex

        async with AsyncSession(engine()) as session:
            thread = await self.threads.get(key)
            todo = Todo(
                todo_id=todo_id,
                thread_id=str(thread.id),
                title=title,
                active_form=active_form,
                assignee=assignee,
            )
            session.add(todo)
            await session.commit()

        self.todos[todo_id] = todo
        return todo

    async def update_todo(self, todo_id: str, status: str) -> bool:
        item = self.todos.get(todo_id)
        if item is None:
            return False
        self.todos[todo_id] = item.model_copy(
            update={"status": status, "updated_at": datetime.now()}
        )

        async with AsyncSession(engine()) as session:
            await session.execute(
                update(TodoModel)
                .where(TodoModel.todo_id == todo_id)
                .values(status=status, updated_at=datetime.now())
            )
            await session.commit()

        return True
