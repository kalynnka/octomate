from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.result import StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests
from slack_sdk.models.messages.chunk import TaskUpdateChunk

from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.feelers.output import (
    BatchedTextUpdate,
    IMMessageID,
    JsonValue,
    MarkdownFeeler,
    TextStreamBatcher,
    markdown_from_output,
    render_stream_event_delta,
)
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.ink import SlackInk

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=JsonValue | DeferredToolRequests)


TASK_TITLES = {
    "answer": "Answer",
    "thinking": "Thinking",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
    "subagent": "Agent run",
}


class SlackMarkdownStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: SlackInk,
        chromo: SlackChromo,
        stream_config: ChannelStreamConfig,
        markdown_feeler: MarkdownFeeler,
        channel_id: str,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.stream_config = stream_config
        self.markdown_feeler = markdown_feeler
        self.channel_id = channel_id

    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, OutputT],
    ) -> IMMessageID | None:
        context = self.chromo.thread_context(key)
        channel = key.chat_id or key.user_id
        output: OutputT | None = None
        message_id: IMMessageID | None = None
        batcher = TextStreamBatcher(
            flush_interval=self.stream_config.flush_interval,
            min_chars=self.stream_config.min_chars,
            max_chars=self.stream_config.max_chars,
            fold_threshold=self.stream_config.fold_threshold,
        )

        try:
            slack_stream = await self.ink.start_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
            )
            try:
                appended = False
                previous_markdown = ""
                try:
                    async for streamed_output in stream.stream_output(debounce_by=None):
                        output = streamed_output
                        markdown = markdown_from_output(streamed_output)
                        if markdown is None:
                            continue
                        delta_text = (
                            markdown[len(previous_markdown) :]
                            if markdown.startswith(previous_markdown)
                            else markdown
                        )
                        previous_markdown = markdown
                        for update in batcher.push_text(delta_text):
                            logger.debug(
                                "Channel %s: streaming Slack delta chars=%d sequence=%d",
                                self.channel_id,
                                len(update.delta_text),
                                update.sequence,
                            )
                            appended = True
                            await self.ink.append_stream(
                                slack_stream,
                                update.delta_text,
                            )
                except Exception:
                    output = await stream.get_output()

                for update in batcher.finish_all():
                    logger.debug(
                        "Channel %s: streaming Slack delta chars=%d sequence=%d",
                        self.channel_id,
                        len(update.delta_text),
                        update.sequence,
                    )
                    appended = True
                    await self.ink.append_stream(slack_stream, update.delta_text)

                markdown = markdown_from_output(output)
                if markdown is not None and not appended:
                    for message in self.chromo.squirt(AgentRunResult(markdown)):
                        await self.ink.append_stream(
                            slack_stream,
                            message.markdown_text or message.text,
                        )
                message_id = await self.ink.stop_stream(slack_stream)
            except Exception:
                try:
                    message_id = await self.ink.stop_stream(slack_stream)
                finally:
                    raise
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack response",
                self.channel_id,
                exc_info=True,
            )
            if output is not None:
                markdown = markdown_from_output(output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
            elif fallback_text := batcher.full_text():
                await self.markdown_feeler.present(key, fallback_text)
        return message_id


class SlackEventStreamFeeler(Generic[OutputT]):
    def __init__(
        self,
        *,
        ink: SlackInk,
        chromo: SlackChromo,
        markdown_feeler: MarkdownFeeler,
        channel_id: str,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.markdown_feeler = markdown_feeler
        self.channel_id = channel_id

    async def present(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[OutputT]],
    ) -> IMMessageID | None:
        context = self.chromo.thread_context(key)
        channel = key.chat_id or key.user_id
        result_event: AgentRunResultEvent[OutputT] | None = None
        batcher = TextStreamBatcher(flush_interval=0, min_chars=0)
        saw_answer = False
        message_id: IMMessageID | None = None

        try:
            stream = await self.ink.start_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
                task_display_mode="timeline",
            )
            try:
                async for event in events:
                    if result_event is not None:
                        continue

                    delta = render_stream_event_delta(event)
                    if delta is not None and delta.text:
                        for update in batcher.push_text(
                            delta.text,
                            block=delta.block,
                        ):
                            saw_answer = saw_answer or update.block_type == "answer"
                            await self.ink.append_stream_chunks(
                                stream,
                                [task_update_chunk(update)],
                            )

                    if isinstance(event, AgentRunResultEvent):
                        result_event = event

                for update in batcher.finish_all():
                    saw_answer = saw_answer or update.block_type == "answer"
                    await self.ink.append_stream_chunks(stream, [task_update_chunk(update)])

                if (
                    result_event is not None
                    and not saw_answer
                    and isinstance(result_event.result.output, str)
                ):
                    await self.ink.append_stream_chunks(
                        stream,
                        [
                            TaskUpdateChunk(
                                id="answer-final",
                                title="Answer",
                                status="complete",
                                output=result_event.result.output,
                            )
                        ],
                    )
                message_id = await self.ink.stop_stream(stream)
            except Exception:
                try:
                    message_id = await self.ink.stop_stream(stream)
                finally:
                    raise
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack event details",
                self.channel_id,
                exc_info=True,
            )
            if result_event is not None and not isinstance(
                result_event.result.output,
                DeferredToolRequests,
            ):
                markdown = markdown_from_output(result_event.result.output)
                if markdown is not None:
                    await self.markdown_feeler.present(key, markdown)
            elif fallback_text := batcher.full_text():
                await self.markdown_feeler.present(key, fallback_text)
        return message_id


def task_update_chunk(update: BatchedTextUpdate) -> TaskUpdateChunk:
    title = update.title or TASK_TITLES[update.block_type]
    status = slack_task_status(update.status)
    if update.block_type == "answer":
        return TaskUpdateChunk(
            id=update.block_id,
            title=title,
            status=status,
            output=update.full_text,
        )
    return TaskUpdateChunk(
        id=update.block_id,
        title=title,
        status=status,
        details=update.full_text,
    )


def slack_task_status(status: str) -> str:
    if status == "done":
        return "complete"
    if status == "error":
        return "error"
    return "in_progress"
