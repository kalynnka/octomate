"""Base channel feeler aggregate surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    GatewayEvent,
    RunErrorEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import channel_logfire
from octomate.tentacles.feelers.deferred import (
    ApprovalFeeler,
    QuestionFeeler,
)
from octomate.tentacles.feelers.oauth import OAuthFeeler
from octomate.tentacles.feelers.output import (
    IMMessageID,
    MarkdownFeeler,
    SegmentsFeeler,
    TimelineFeeler,
)

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager


@dataclass
class Feelers:
    """Per-channel human-interaction surface."""

    markdown: MarkdownFeeler
    timeline: TimelineFeeler
    segments: SegmentsFeeler
    approvals: ApprovalFeeler
    ask_questions: QuestionFeeler
    oauth: OAuthFeeler

    async def present(
        self, address: ChannelAddress, event: GatewayEvent | RunErrorEvent
    ) -> IMMessageID | None:
        """What the graph reports outside a run, as a message: a move's
        announcement, or a failure's trace id. A move that leaves no line says
        nothing here."""
        if isinstance(event, RunErrorEvent):
            return await self.markdown.present(
                address,
                "Something went wrong while handling your message. "
                f"Reference id for tracing the issue: `{event.trace_id}`.",
            )
        if event.announcement is None:
            return None
        return await self.markdown.present(address, event.announcement)

    async def present_actions(
        self,
        address: ChannelAddress,
        event: ActionBatchEvent,
        *,
        action_manager: DeferredActionManager,
    ) -> None:
        """A batch put up for a run that is not streamed, as the channel's cards."""
        with channel_logfire.span(
            "present_actions",
            target_address=str(address),
            approvals=len(event.approvals),
            questions=len(event.questions),
        ):
            approval_message_ids = await self.approvals.present(
                address, event.approvals
            )
            for action in event.approvals:
                await action_manager.mark_action_presented(
                    action.id,
                    approval_message_ids.get(action.id),
                )

            message_ids = await self.ask_questions.present(address, event.questions)
            for action in event.questions:
                await action_manager.mark_action_presented(
                    action.id,
                    message_ids.get(action.id),
                )
