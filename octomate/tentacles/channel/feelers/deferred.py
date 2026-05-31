"""Plain-text fallbacks for human-in-the-loop deferred action feelers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import (
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.channel.feelers.output import IMMessageID, MarkdownFeeler


class ApprovalFeeler(ABC):
    """Presents approval actions for one response target."""

    @abstractmethod
    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]: ...


class QuestionFeeler(ABC):
    """Presents a card-based question wizard for one response target."""

    @abstractmethod
    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]: ...


class PlainTextApprovalFeeler(ApprovalFeeler):
    def __init__(self, markdown: MarkdownFeeler) -> None:
        self.markdown = markdown

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]:
        message_ids: dict[UUID, IMMessageID | None] = {}
        for action in actions:
            message_ids[action.id] = await self.markdown.present(
                key,
                (
                    f"Octomate needs approval for `{action.tool_name}` "
                    f"({action.id}). This channel can show the request, but does "
                    "not support interactive approval cards yet."
                ),
            )
        return message_ids


class PlainTextAskQuestionFeeler(QuestionFeeler):
    def __init__(self, markdown: MarkdownFeeler) -> None:
        self.markdown = markdown

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]:
        message_ids: dict[UUID, IMMessageID | None] = {}
        for action in actions:
            choices_text = ""
            if choices := action.args.get("choices"):
                choices_text = "\nChoices:\n" + "\n".join(
                    f"- {choice}" for choice in choices
                )
            hint = action.args.get("hint") or ""
            hint_text = f"\nHint: {hint}" if hint else ""
            message_ids[action.id] = await self.markdown.present(
                key,
                (
                    f"Octomate needs an answer: {action.args['question']}"
                    f"{hint_text}{choices_text}\nDeferred action: `{action.id}`"
                ),
            )
        return message_ids
