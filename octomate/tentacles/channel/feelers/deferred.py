"""Plain-text fallbacks for human-in-the-loop deferred action feelers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from uuid import UUID

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    DeferredApproval,
    DeferredQuestion,
)
from octomate.telemetry import channel_logfire
from octomate.tentacles.channel.feelers.output import IMMessageID, MarkdownFeeler

QUESTION_PROGRESS_SUFFIX = re.compile(
    r"\s*\(?\s*Question\s+\d+\s+of\s+\d+\s*\)?\s*$",
    re.IGNORECASE,
)


def question_text(action: DeferredQuestion) -> str:
    text = str(action.args["question"]).strip()
    cleaned = QUESTION_PROGRESS_SUFFIX.sub("", text).strip()
    return cleaned or text


class ApprovalFeeler(ABC):
    """Presents approval actions for one response target."""

    @abstractmethod
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]: ...


class QuestionFeeler(ABC):
    """Presents a card-based question wizard for one response target."""

    @abstractmethod
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]: ...


class PlainTextApprovalFeeler(ApprovalFeeler):
    def __init__(self, markdown: MarkdownFeeler) -> None:
        self.markdown = markdown

    @channel_logfire.instrument("plaintext.approvals.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]:
        message_ids: dict[UUID, IMMessageID | None] = {}
        for action in actions:
            message_ids[action.id] = await self.markdown.present(
                address,
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

    @channel_logfire.instrument("plaintext.ask_questions.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
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
                address,
                (
                    f"Octomate needs an answer: {question_text(action)}"
                    f"{hint_text}{choices_text}\nDeferred action: `{action.id}`"
                ),
            )
        return message_ids
