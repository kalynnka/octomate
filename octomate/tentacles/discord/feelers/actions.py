"""Discord component routing.

Resolves button and modal interactions into deferred-action responses, with
one lock per batch so concurrent clicks on it are serialized.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar
from weakref import WeakKeyDictionary, WeakValueDictionary

import discord
from pydantic import UUID7

from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.deferred import (
    DeferredApproval,
    DeferredQuestion,
    QuestionAnswer,
)

if TYPE_CHECKING:
    from octomate.base import Octomate


class DiscordActionUnavailable(ValueError):
    """An interaction on a batch or action that is gone or already handled; the
    message is what the user sees, ephemerally."""


@dataclass(frozen=True)
class DiscordChoiceAnswer:
    """A question answered by picking a choice, by index into its choices."""

    index: int

    def resolve(self, choices: list[str]) -> str:
        if not 0 <= self.index < len(choices):
            raise DiscordActionUnavailable("This answer choice is no longer available.")
        return choices[self.index]


class DiscordComponentRouter:
    """Per-client router from component interactions to deferred-action
    responses, holding one lock per batch and the answers of a wizard still in
    progress."""

    routers: ClassVar[WeakKeyDictionary[discord.Client, DiscordComponentRouter]] = (
        WeakKeyDictionary()
    )

    def __init__(self, octomate: Octomate) -> None:
        self.octomate = octomate
        self.callback_locks: WeakValueDictionary[UUID7, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self.question_answers: dict[UUID7, dict[UUID7, QuestionAnswer]] = {}

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
        batch_id: UUID7,
        action_id: UUID7,
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
        batch_id: UUID7,
    ) -> tuple[list[DeferredQuestion], dict[UUID7, QuestionAnswer]]:
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
        batch_id: UUID7,
        action_id: UUID7,
        answer: str | DiscordChoiceAnswer | list[DiscordChoiceAnswer],
    ) -> tuple[list[DeferredQuestion], dict[UUID7, QuestionAnswer]]:
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

            choices = action.args.get("choices") or []
            if isinstance(answer, DiscordChoiceAnswer):
                resolved_answer: QuestionAnswer = answer.resolve(choices)
            elif isinstance(answer, list):
                resolved_answer = [pick.resolve(choices) for pick in answer]
            else:
                resolved_answer = answer
            answers[action_id] = resolved_answer
            return actions, answers

    async def submit_questions(
        self,
        *,
        batch_id: UUID7,
        responder_id: str,
        settle_message: Callable[
            [list[DeferredQuestion], dict[UUID7, QuestionAnswer]], Awaitable[None]
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
                if action.status == "answered" and isinstance(action.result, str | list)
            },
            approvals={
                action.id: action.result
                for action in batch.approvals
                if action.status in {"approved", "denied"}
                and isinstance(action.result, bool)
            },
        )
