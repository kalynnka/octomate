from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar
from weakref import WeakKeyDictionary, WeakValueDictionary

import discord

from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.deferred import (
    DeferredApproval,
    DeferredQuestion,
)

if TYPE_CHECKING:
    from octomate.base import Octomate


class DiscordActionUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class DiscordChoiceAnswer:
    index: int


class DiscordComponentRouter:
    routers: ClassVar[WeakKeyDictionary[discord.Client, DiscordComponentRouter]] = (
        WeakKeyDictionary()
    )

    def __init__(self, octomate: Octomate) -> None:
        self.octomate = octomate
        self.callback_locks: WeakValueDictionary[uuid.UUID, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self.question_answers: dict[uuid.UUID, dict[uuid.UUID, str]] = {}

    def bind(self, client: discord.Client) -> None:
        self.routers[client] = self

    @classmethod
    def for_client(cls, client: discord.Client) -> DiscordComponentRouter:
        router = cls.routers.get(client)
        if router is None:
            raise RuntimeError("Discord client has no component router")
        return router

    async def resolve_approval(
        self,
        *,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        responder_id: str,
        approved: bool,
        settle_message: Callable[[DeferredApproval], Awaitable[None]],
    ) -> DeferredApproval:
        callback_lock = self.callback_locks.setdefault(batch_id, asyncio.Lock())
        async with callback_lock:
            try:
                batch = await self.octomate.deferred_actions.get_batch(batch_id)
            except ValueError as error:
                raise DiscordActionUnavailable(
                    "This approval is no longer available."
                ) from error
            action = next(
                (
                    candidate
                    for candidate in batch.approvals
                    if candidate.id == action_id
                ),
                None,
            )
            if action is None or action.batch_id != batch_id:
                raise DiscordActionUnavailable(
                    "This approval does not belong to this request."
                )
            if batch.status != "pending" or action.status != "pending":
                raise DiscordActionUnavailable("This approval was already handled.")

            dispatch = await self.resolve(
                DeferredActionBatchResponse(
                    batch_id=batch_id,
                    responder_id=responder_id,
                    approvals={action_id: approved},
                )
            )
        await settle_message(action)
        if dispatch is not None:
            await self.octomate.kick(dispatch)
        return action

    async def load_questions(
        self,
        batch_id: uuid.UUID,
    ) -> tuple[list[DeferredQuestion], dict[uuid.UUID, str]]:
        try:
            batch = await self.octomate.deferred_actions.get_batch(batch_id)
        except ValueError as error:
            raise DiscordActionUnavailable(
                "These questions are no longer available."
            ) from error
        if batch.status != "pending" or any(
            action.status != "pending" for action in batch.questions
        ):
            raise DiscordActionUnavailable("These questions were already submitted.")
        return sorted(batch.questions), self.question_answers.setdefault(batch_id, {})

    async def save_question_answer(
        self,
        *,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        answer: str | DiscordChoiceAnswer,
    ) -> tuple[list[DeferredQuestion], dict[uuid.UUID, str]]:
        callback_lock = self.callback_locks.setdefault(batch_id, asyncio.Lock())
        async with callback_lock:
            actions, answers = await self.load_questions(batch_id)
            action = next(
                (candidate for candidate in actions if candidate.id == action_id),
                None,
            )
            if action is None or action.batch_id != batch_id:
                raise DiscordActionUnavailable(
                    "This question does not belong to this request."
                )
            if action.status != "pending":
                raise DiscordActionUnavailable(
                    "These questions were already submitted."
                )

            if isinstance(answer, DiscordChoiceAnswer):
                choices = action.args.get("choices") or []
                if not 0 <= answer.index < len(choices):
                    raise DiscordActionUnavailable(
                        "This answer choice is no longer available."
                    )
                resolved_answer = choices[answer.index]
            else:
                resolved_answer = answer
            answers[action_id] = resolved_answer
            return actions, answers

    async def submit_questions(
        self,
        *,
        batch_id: uuid.UUID,
        responder_id: str,
        settle_message: Callable[
            [list[DeferredQuestion], dict[uuid.UUID, str]], Awaitable[None]
        ],
    ) -> list[DeferredQuestion]:
        callback_lock = self.callback_locks.setdefault(batch_id, asyncio.Lock())
        async with callback_lock:
            actions, stored_answers = await self.load_questions(batch_id)
            answers = dict(stored_answers)
            if any(action.id not in answers for action in actions):
                raise DiscordActionUnavailable(
                    "Answer every question before submitting."
                )
            dispatch = await self.resolve(
                DeferredActionBatchResponse(
                    batch_id=batch_id,
                    responder_id=responder_id,
                    answers={
                        action.id: answers.get(action.id, "") for action in actions
                    },
                )
            )
            self.question_answers.pop(batch_id, None)
        await settle_message(actions, answers)
        if dispatch is not None:
            await self.octomate.kick(dispatch)
        return actions

    async def resolve(
        self,
        response: DeferredActionBatchResponse,
    ) -> DeferredActionBatchResponse | None:
        batch = await self.octomate.deferred_actions.resolve_batch(response)
        if not batch.completed:
            return None
        return DeferredActionBatchResponse(
            batch_id=batch.id,
            responder_id=response.responder_id,
            answers={
                action.id: action.result
                for action in batch.questions
                if action.status == "answered" and isinstance(action.result, str)
            },
            approvals={
                action.id: action.result
                for action in batch.approvals
                if action.status in {"approved", "denied"}
                and isinstance(action.result, bool)
            },
        )
