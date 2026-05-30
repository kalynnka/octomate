from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar

import lark_oapi
import lark_oapi.ws.client as ws_mod
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from pydantic import ValidationError
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.tools import DeferredToolRequests

from octomate.config import LarkChannelConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.base import ChannelTentacle, ThreadStrategy
from octomate.tentacles.channel.feelers import Feelers
from octomate.tentacles.channel.lark.chromo import (
    LARK_STREAM_ELEMENT_ID,
    LarkChromo,
)
from octomate.tentacles.channel.lark.feelers import (
    LarkApprovalFeeler,
    LarkAskQuestionFeeler,
    LarkCardAction,
    LarkQuestionActionsAdapter,
    approval_resolution_card_data,
    ask_question_card_data,
    collect_answer,
    submitted_card_data,
)
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage, LarkStreamCard
from octomate.tentacles.channel.stream import (
    BatchedTextUpdate,
    TextStreamBatcher,
    render_text_stream_delta,
)

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


def log_card_action_result(channel_id: str, task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "Channel %s: failed to handle Lark card action",
            channel_id,
            exc_info=(type(error), error, error.__traceback__),
        )


class LarkTentacle(ChannelTentacle):
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    octomate: Octomate

    ws_client: lark_oapi.ws.Client
    stop_event: asyncio.Event | None
    ping_task: asyncio.Task[None] | None

    def __init__(
        self,
        id: str,
        octomate: Octomate | None,
        *,
        config: LarkChannelConfig,
    ) -> None:
        if octomate is None:
            raise ValueError(f"channel {id!r} requires an attached Octomate")
        self.ink = LarkInk(config.app_id, config.app_secret)
        self.chromo = LarkChromo()

        event_handler = (
            lark_oapi.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.sense)
            .register_p2_card_action_trigger(self.on_card_action)
            .build()
        )
        self.ws_client = lark_oapi.ws.Client(
            self.ink.app_id,
            self.ink.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark_oapi.LogLevel.INFO,
        )
        self.stop_event = None
        self.ping_task = None
        super().__init__(
            id=id,
            octomate=octomate,
            ink=self.ink,
            chromo=self.chromo,
            config=config,
        )
        self.feelers = Feelers(
            approvals=LarkApprovalFeeler(self.ink),
            ask_questions=LarkAskQuestionFeeler(self.ink),
        )

    async def activate(self) -> None:
        logger.info("Channel %s: starting Lark WebSocket client", self.id)
        # lark-oapi exposes a blocking public start(), but its WebSocket client
        # is async internally. Run those internals on Octomate's event loop so
        # message callbacks can schedule ingest directly.
        ws_mod.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        await self.ws_client._connect()  # type: ignore[attr-defined]
        self.ping_task = asyncio.create_task(self.ws_client._ping_loop())  # type: ignore[attr-defined]
        try:
            await self.stop_event.wait()
        finally:
            await self._disconnect()

    async def deactivate(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        await self._disconnect()

    async def respond(
        self,
        key: ConversationKey,
        result: AgentRunResult[Any],
    ) -> None:
        chat_id = key.chat_id or key.user_id
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        reply_in_thread = reply_to is not None
        messages = self.chromo.squirt(result)
        if messages:
            await self.ink.send_message(
                chat_id,
                key.chat_type,
                messages,
                reply_to,
                reply_in_thread=reply_in_thread,
            )

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
    ) -> None:
        chat_id = key.chat_id or key.user_id
        reply_to = key.thread_id if key.thread_id.startswith("om_") else None
        reply_in_thread = reply_to is not None
        batcher = TextStreamBatcher(
            flush_interval=self.config.stream.flush_interval,
            min_chars=self.config.stream.min_chars,
            max_chars=self.config.stream.max_chars,
            fold_threshold=self.config.stream.fold_threshold,
        )
        card: LarkStreamCard | None = None
        stream_started = False
        final_messages: list[LarkOutboundMessage] = []
        result_event: AgentRunResultEvent[Any] | None = None

        async def apply_update(update: BatchedTextUpdate) -> None:
            nonlocal card, stream_started
            if card is None:
                card_data = self.chromo.make_stream_card_data(
                    "",
                    element_id=LARK_STREAM_ELEMENT_ID,
                )
                card = await self.ink.create_stream_card(
                    card_data,
                    element_id=LARK_STREAM_ELEMENT_ID,
                )
                if card is None:
                    raise RuntimeError("failed to create Lark stream card")
                msg_id = await self.ink.send_stream_card(
                    chat_id,
                    key.chat_type,
                    card,
                    reply_to=reply_to,
                    reply_in_thread=reply_in_thread,
                )
                if msg_id is None:
                    raise RuntimeError("failed to send Lark stream card")
                stream_started = True

            if not await self.ink.update_stream_card(
                card,
                content=update.full_text,
                sequence=update.sequence,
            ):
                raise RuntimeError("failed to update Lark stream card")

        try:
            async for event in events:
                if result_event is not None:
                    continue

                delta = render_text_stream_delta(event)
                if delta:
                    for update in batcher.push_text(delta):
                        await apply_update(update)

                if isinstance(event, AgentRunResultEvent):
                    result_event = event

            is_deferred_result = result_event is not None and isinstance(
                result_event.result.output,
                DeferredToolRequests,
            )
            if result_event is not None and not is_deferred_result:
                final_messages = self.chromo.squirt(result_event.result)
            for update in batcher.finish_all():
                await apply_update(update)
            if (
                result_event is not None
                and not is_deferred_result
                and final_messages
                and not stream_started
            ):
                await self.ink.send_message(
                    chat_id,
                    key.chat_type,
                    final_messages,
                    reply_to,
                    reply_in_thread=reply_in_thread,
                )
        except Exception:
            logger.warning(
                "Channel %s: failed to stream Lark response",
                self.id,
                exc_info=True,
            )
            if final_messages:
                await self.ink.send_message(
                    chat_id,
                    key.chat_type,
                    final_messages,
                    reply_to,
                    reply_in_thread=reply_in_thread,
                )
                return
            fallback_text = batcher.full_text()
            if fallback_text:
                await self.ink.send_message(
                    chat_id,
                    key.chat_type,
                    [self.chromo.make_markdown_message(fallback_text)],
                    reply_to,
                    reply_in_thread=reply_in_thread,
                )

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        message_id = await self.ink.send_message(
            key.chat_id or key.user_id,
            key.chat_type,
            [self.chromo.make_markdown_message(hint_text)],
            None,
        )
        return replace(key, thread_id=message_id or key.thread_id)

    def sense(self, data: P2ImMessageReceiveV1) -> None:
        task = asyncio.create_task(self.ingest(data))

        def log_result(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                logger.error(
                    "Channel %s: failed to handle Lark message",
                    self.id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(log_result)

    def on_card_action(
        self,
        data: P2CardActionTrigger,
    ) -> P2CardActionTriggerResponse:
        if data.event is None or data.event.action is None:
            return P2CardActionTriggerResponse({})
        callback_action = data.event.action
        value = callback_action.value or {}
        try:
            action = LarkCardAction(str(value.get("action") or ""))
        except ValueError:
            return P2CardActionTriggerResponse({})
        responder_id = ""
        if data.event.operator is not None:
            responder_id = (
                data.event.operator.open_id or data.event.operator.user_id or ""
            )

        if action in {
            LarkCardAction.APPROVAL_APPROVE,
            LarkCardAction.APPROVAL_DENY,
        }:
            action_id = value.get("action_id")
            batch_id = value.get("batch_id")
            if not action_id or not batch_id:
                return P2CardActionTriggerResponse({})
            approved = action == LarkCardAction.APPROVAL_APPROVE
            task = asyncio.create_task(
                self.octomate.kick(
                    DeferredActionBatchResponse(
                        batch_id=uuid.UUID(str(batch_id)),
                        responder_id=responder_id,
                        approvals={uuid.UUID(str(action_id)): approved},
                    )
                )
            )
            task.add_done_callback(
                lambda task: log_card_action_result(self.id, task)
            )
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "success",
                        "content": "Approved" if approved else "Denied",
                    },
                    "card": {
                        "type": "raw",
                        "data": approval_resolution_card_data(
                            tool_name=str(value.get("tool_name") or "Tool call"),
                            approved=approved,
                        ),
                    },
                }
            )

        if action in {
            LarkCardAction.ASK_QUESTION_BACK,
            LarkCardAction.ASK_QUESTION_NEXT,
            LarkCardAction.ASK_QUESTION_SUBMIT,
        }:
            try:
                question_actions = LarkQuestionActionsAdapter.validate_python(
                    value.get("questions") or []
                )
            except ValidationError:
                return P2CardActionTriggerResponse({})
            if not question_actions:
                return P2CardActionTriggerResponse({})
            page = int(value.get("page") or 0)
            answers = collect_answer(
                question_actions,
                page,
                callback_action.form_value or {},
                value.get("answers") or {},
            )
            if action == LarkCardAction.ASK_QUESTION_BACK:
                page -= 1
            elif action == LarkCardAction.ASK_QUESTION_NEXT:
                page += 1
            else:
                batch_id = str(value.get("batch_id") or question_actions[0].batch_id)
                task = asyncio.create_task(
                    self.octomate.kick(
                        DeferredActionBatchResponse(
                            batch_id=uuid.UUID(batch_id),
                            responder_id=responder_id,
                            answers={
                                question.id: str(answers.get(str(question.id), ""))
                                for question in question_actions
                            },
                        )
                    )
                )
                task.add_done_callback(
                    lambda task: log_card_action_result(self.id, task)
                )
                return P2CardActionTriggerResponse(
                    {
                        "toast": {"type": "success", "content": "Answers submitted"},
                        "card": {
                            "type": "raw",
                            "data": submitted_card_data(question_actions),
                        },
                    }
                )
            return P2CardActionTriggerResponse(
                {
                    "toast": {"type": "success", "content": "Received"},
                    "card": {
                        "type": "raw",
                        "data": ask_question_card_data(
                            actions=question_actions,
                            page=page,
                            answers=answers,
                        ),
                    },
                }
            )

        return P2CardActionTriggerResponse({})

    async def close(self) -> None:
        await self.deactivate()
        self.ink.close()

    async def _disconnect(self) -> None:
        self.ws_client._auto_reconnect = False  # type: ignore[attr-defined]
        if self.ping_task is not None:
            ping_task = self.ping_task
            self.ping_task = None
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        await self.ws_client._disconnect()  # type: ignore[attr-defined]
