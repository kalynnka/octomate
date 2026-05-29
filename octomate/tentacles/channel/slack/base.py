from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import ValidationError
from pydantic_ai import AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.tools import DeferredToolRequests
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncSay

from octomate.config import SlackChannelConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.base import ChannelTentacle, ThreadStrategy
from octomate.tentacles.channel.feelers import Feelers
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.feelers import (
    SlackBlockAction,
    SlackQuestionActionsAdapter,
    SlackApprovalFeeler,
    SlackAskQuestionFeeler,
    approval_resolution_blocks,
    ask_question_blocks,
    collect_current_answer,
    submitted_blocks,
)
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import (
    SlackAssistantThreadEvent,
    SlackMessageEvent,
    SlackOutboundMessage,
)
from octomate.tentacles.channel.stream import TextStreamBatcher

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

IGNORED_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
    }
)


class SlackTentacle(ChannelTentacle):
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    octomate: Octomate

    def __init__(
        self,
        id: str,
        octomate: Octomate | None,
        *,
        config: SlackChannelConfig,
    ) -> None:
        if octomate is None:
            raise ValueError(f"channel {id!r} requires an attached Octomate")
        self.app_id = config.app_id
        self.ink = SlackInk(config.bot_token)
        self.chromo = SlackChromo()
        self.app = AsyncApp(token=config.bot_token.get_secret_value())
        self.app.event("message")(self.on_message)
        self.app.event("assistant_thread_started")(self.on_assistant_thread_started)
        self.app.event("assistant_thread_context_changed")(
            self.on_assistant_thread_context_changed
        )
        self.app.action(SlackBlockAction.APPROVAL_APPROVE.value)(
            self.on_approval_action
        )
        self.app.action(SlackBlockAction.APPROVAL_DENY.value)(self.on_approval_action)
        self.app.action(SlackBlockAction.ASK_QUESTION_BACK.value)(
            self.on_question_nav
        )
        self.app.action(SlackBlockAction.ASK_QUESTION_NEXT.value)(
            self.on_question_nav
        )
        self.app.action(SlackBlockAction.ASK_QUESTION_SUBMIT.value)(
            self.on_question_nav
        )
        self.app.action(SlackBlockAction.ASK_QUESTION_CHOICE.value)(
            self.on_question_choice
        )
        self.app_token = config.app_token
        self.handler: AsyncSocketModeHandler | None = None
        super().__init__(
            id=id,
            octomate=octomate,
            ink=self.ink,
            chromo=self.chromo,
            config=config,
        )
        self.feelers = Feelers(
            approvals=SlackApprovalFeeler(self.ink),
            ask_questions=SlackAskQuestionFeeler(self.ink),
        )

    async def activate(self) -> None:
        logger.info("Channel %s: starting Slack Socket Mode client", self.id)
        self.handler = AsyncSocketModeHandler(
            self.app,
            self.app_token.get_secret_value(),
        )
        await self.handler.start_async()

    async def deactivate(self) -> None:
        if self.handler:
            await self.handler.close_async()
            self.handler = None

    async def on_message(self, event: SlackMessageEvent, say: AsyncSay) -> None:
        subtype = event.get("subtype")
        if subtype in IGNORED_SUBTYPES:
            return
        if event.get("bot_id") or event.get("user") == self.profile.user_id:
            return
        await self.ingest(event)

    async def on_assistant_thread_started(
        self,
        event: SlackAssistantThreadEvent,
    ) -> None:
        await self.ensure_assistant_thread(event)

    async def on_assistant_thread_context_changed(
        self,
        event: SlackAssistantThreadEvent,
    ) -> None:
        await self.ensure_assistant_thread(event)

    async def on_approval_action(self, ack, body: dict[str, Any]) -> None:
        await ack()
        action_body = _first_action_value(body)
        action_id = action_body.get("action_id")
        batch_id = action_body.get("batch_id")
        if not action_id or not batch_id:
            return
        responder_id = body.get("user", {}).get("id", "")
        approved = bool(action_body.get("approved"))
        channel = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")
        tool_name = str(action_body.get("tool_name") or "Tool call")
        if channel and message_ts:
            await self.ink.update_message(
                channel,
                message_ts,
                text=f"{tool_name} - {'Approved' if approved else 'Denied'}",
                blocks=approval_resolution_blocks(
                    tool_name=tool_name,
                    approved=approved,
                    responder_id=responder_id,
                ),
            )
        await self.octomate.kick(
            DeferredActionBatchResponse(
                batch_id=uuid.UUID(str(batch_id)),
                responder_id=responder_id,
                approvals={uuid.UUID(str(action_id)): approved},
            )
        )

    async def on_question_nav(self, ack, body: dict[str, Any]) -> None:
        await ack()
        action_value = _first_action_value(body)
        try:
            actions = SlackQuestionActionsAdapter.validate_python(
                action_value.get("questions") or []
            )
        except ValidationError:
            return
        if not actions:
            return
        page = int(action_value.get("page") or 0)
        answers = dict(action_value.get("answers") or {})
        answers = collect_current_answer(body, actions, page, answers)
        action_id = (body.get("actions") or [{}])[0].get("action_id", "")
        if action_id == SlackBlockAction.ASK_QUESTION_BACK:
            page -= 1
        elif action_id == SlackBlockAction.ASK_QUESTION_NEXT:
            page += 1
        else:
            batch_id = str(action_value.get("batch_id") or actions[0].batch_id)
            await self.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=uuid.UUID(batch_id),
                    responder_id=body.get("user", {}).get("id", ""),
                    answers={
                        action.id: str(answers.get(str(action.id), ""))
                        for action in actions
                    },
                )
            )
            channel = body.get("channel", {}).get("id", "")
            message_ts = body.get("message", {}).get("ts", "")
            if channel and message_ts:
                await self.ink.update_message(
                    channel,
                    message_ts,
                    text="Answers submitted",
                    blocks=submitted_blocks(actions),
                )
            return
        channel = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")
        if channel and message_ts:
            await self.ink.update_message(
                channel,
                message_ts,
                text="Questions needed",
                blocks=ask_question_blocks(
                    actions,
                    page=page,
                    answers=answers,
                ),
            )

    async def on_question_choice(self, ack) -> None:
        await ack()

    async def ensure_assistant_thread(
        self,
        event: SlackAssistantThreadEvent,
    ) -> None:
        thread = event.get("assistant_thread", {})
        channel_id = thread.get("channel_id", "")
        thread_ts = thread.get("thread_ts", "")
        user_id = thread.get("user_id", "")
        if not channel_id or not thread_ts or not user_id:
            logger.debug(
                "Channel %s: ignored incomplete Slack assistant thread event %s",
                self.id,
                event,
            )
            return

        key = ConversationKey(
            channel_tentacle_id=self.id,
            chat_type="private",
            chat_id=channel_id,
            user_id=user_id,
            thread_id=thread_ts,
        )
        with sqlalchemy_materia():
            await self.octomate.conversations.ensure(
                key,
                agent_tentacle_id=self.agent_id,
            )
        logger.info("Channel %s: ensured Slack assistant thread %s", self.id, key)

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
    ) -> None:
        context = self.chromo.thread_context(key)
        channel = key.chat_id or key.user_id
        final_messages: list[SlackOutboundMessage] = []
        result_event: AgentRunResultEvent[Any] | None = None
        batcher = TextStreamBatcher(
            flush_interval=self.config.stream.flush_interval,
            min_chars=self.config.stream.min_chars,
            max_chars=self.config.stream.max_chars,
            fold_threshold=self.config.stream.fold_threshold,
        )

        try:
            async with self.ink.open_stream(
                channel,
                context.thread_ts,
                recipient_user_id=context.recipient_user_id,
                recipient_team_id=context.recipient_team_id,
            ) as stream:
                appended = False
                async for event in events:
                    if result_event is not None:
                        continue

                    delta = self.chromo.render_stream_delta(event)
                    if delta:
                        for update in batcher.push_text(delta):
                            logger.debug(
                                "Channel %s: streaming Slack delta chars=%d sequence=%d",
                                self.id,
                                len(update.delta_text),
                                update.sequence,
                            )
                            await self.ink.append_stream(stream, update.delta_text)
                            appended = True

                    if isinstance(event, AgentRunResultEvent):
                        result_event = event

                is_deferred_result = result_event is not None and isinstance(
                    result_event.result.output,
                    DeferredToolRequests,
                )
                if result_event is not None and not is_deferred_result:
                    final_messages = self.chromo.squirt(result_event.result)
                    final_text = "\n".join(
                        message.markdown_text or message.text
                        for message in final_messages
                    )
                    logger.debug(
                        "Channel %s: Slack stream result chars=%d",
                        self.id,
                        len(final_text),
                    )
                for update in batcher.finish_all():
                    logger.debug(
                        "Channel %s: streaming Slack delta chars=%d sequence=%d",
                        self.id,
                        len(update.delta_text),
                        update.sequence,
                    )
                    await self.ink.append_stream(stream, update.delta_text)
                    appended = True
                if (
                    result_event is not None
                    and not is_deferred_result
                    and final_messages
                    and not appended
                ):
                    for message in final_messages:
                        await self.ink.append_stream(
                            stream,
                            message.markdown_text or message.text,
                        )
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Slack response",
                self.id,
                exc_info=True,
            )
            if final_messages:
                await self.ink.send_message(
                    channel,
                    key.chat_type,
                    final_messages,
                    context.thread_ts,
                )

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        message_id = await self.ink.send_message(
            key.chat_id or key.user_id,
            key.chat_type,
            [SlackOutboundMessage(text=hint_text, markdown_text=hint_text)],
            None,
        )
        return replace(key, thread_id=message_id or key.thread_id)


def _first_action_value(body: dict[str, Any]) -> dict[str, Any]:
    actions = body.get("actions") or []
    if not actions:
        return {}
    try:
        value = json.loads(actions[0].get("value") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
