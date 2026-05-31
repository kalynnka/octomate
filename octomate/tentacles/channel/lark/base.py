from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar

import lark_oapi
import lark_oapi.ws.client as ws_mod
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from pydantic import TypeAdapter, ValidationError

from octomate.config import LarkChannelConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.lark.chromo import LarkChromo
from octomate.tentacles.channel.lark.feelers.actions import LarkCardAction
from octomate.tentacles.channel.lark.feelers.approvals import (
    LarkApprovalFeeler,
    approval_resolution_card_data,
)
from octomate.tentacles.channel.lark.feelers.questions import (
    LarkAskQuestionFeeler,
    ask_question_card_data,
    collect_answer,
    submitted_card_data,
)
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.lark.output import (
    LarkEventStreamFeeler,
    LarkMarkdownFeeler,
    LarkMarkdownStreamFeeler,
)
from octomate.tentacles.channel.lark.schema import (
    LarkApprovalActionValue,
    LarkOutboundMessage,
    LarkQuestionActionValue,
    LarkQuestionFormValue,
)

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)
LarkApprovalActionValueAdapter = TypeAdapter(LarkApprovalActionValue)
LarkQuestionActionValueAdapter = TypeAdapter(LarkQuestionActionValue)
LarkQuestionFormValueAdapter = TypeAdapter(LarkQuestionFormValue)


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


class LarkTentacle(ChannelTentacle[P2ImMessageReceiveV1, LarkOutboundMessage]):
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    octomate: Octomate
    ink: LarkInk
    chromo: LarkChromo
    feelers: Feelers[ChannelOutput]

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
        self.ink = LarkInk(config.app_id, config.app_secret)  # pyright: ignore[reportIncompatibleVariableOverride]
        self.chromo = LarkChromo()  # pyright: ignore[reportIncompatibleVariableOverride]

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
        markdown_feeler = LarkMarkdownFeeler(ink=self.ink, chromo=self.chromo)
        self.feelers = Feelers(
            markdown=markdown_feeler,
            markdown_stream=LarkMarkdownStreamFeeler[ChannelOutput](
                ink=self.ink,
                chromo=self.chromo,
                stream_config=self.config.stream,
                markdown_feeler=markdown_feeler,
                channel_id=self.id,
            ),
            event_stream=LarkEventStreamFeeler[ChannelOutput](
                ink=self.ink,
                markdown_feeler=markdown_feeler,
                channel_id=self.id,
            ),
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
            try:
                action_value = LarkApprovalActionValueAdapter.validate_python(value)
            except ValidationError:
                return P2CardActionTriggerResponse({})
            approved = action == LarkCardAction.APPROVAL_APPROVE
            task = asyncio.create_task(
                self.octomate.kick(
                    DeferredActionBatchResponse(
                        batch_id=action_value["batch_id"],
                        responder_id=responder_id,
                        approvals={action_value["action_id"]: approved},
                    )
                )
            )
            task.add_done_callback(lambda task: log_card_action_result(self.id, task))
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "success",
                        "content": "Approved" if approved else "Denied",
                    },
                    "card": {
                        "type": "raw",
                        "data": approval_resolution_card_data(
                            tool_name=action_value.get("tool_name", "Tool call"),
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
                action_value = LarkQuestionActionValueAdapter.validate_python(value)
                form_value = LarkQuestionFormValueAdapter.validate_python(
                    callback_action.form_value or {}
                )
            except ValidationError:
                return P2CardActionTriggerResponse({})
            question_actions = action_value["questions"]
            page = action_value["page"]
            answers = collect_answer(
                question_actions,
                page,
                form_value,
                action_value["answers"],
            )
            if action == LarkCardAction.ASK_QUESTION_BACK:
                page -= 1
            elif action == LarkCardAction.ASK_QUESTION_NEXT:
                page += 1
            else:
                task = asyncio.create_task(
                    self.octomate.kick(
                        DeferredActionBatchResponse(
                            batch_id=action_value["batch_id"],
                            responder_id=responder_id,
                            answers={
                                question.id: str(answers.get(question.id, ""))
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
