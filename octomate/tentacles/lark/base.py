"""Lark channel tentacle.

Runs the lark-oapi WebSocket client, hands received messages to the ingest
pipeline and acknowledges card replies; persisted events redraw the cards.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Self

import lark_oapi
import lark_oapi.ws.client as ws_mod
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError
from rich.style import Style

from octomate.config import LarkChannelConfig
from octomate.oauth.flows import AuthorizationCodeFlow, OAuthTokenExchange
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.oauth import DirectHttpOAuthCallbackTransport
from octomate.tentacles.channel import (
    ChannelSurfaces,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.feelers.base import Feelers
from octomate.tentacles.feelers.output import DefaultSegmentsFeeler
from octomate.tentacles.lark.chromo import LarkChromo
from octomate.tentacles.lark.feelers.actions import LarkCardAction
from octomate.tentacles.lark.feelers.approvals import (
    LarkApprovalFeeler,
)
from octomate.tentacles.lark.feelers.artifacts import LarkArtifactFeeler
from octomate.tentacles.lark.feelers.oauth import (
    LarkOAuthFeeler,
)
from octomate.tentacles.lark.feelers.output import (
    LarkMarkdownFeeler,
    LarkTimelineFeeler,
)
from octomate.tentacles.lark.feelers.questions import (
    LarkAskQuestionFeeler,
    collect_answers,
)
from octomate.tentacles.lark.ink import LarkInk, LarkWebSocketClient
from octomate.tentacles.lark.oauth import LarkOAuthConnector
from octomate.tentacles.lark.schema import (
    LarkApprovalActionValue,
    LarkOutboundMessage,
    LarkQuestionActionValue,
    LarkQuestionFormValue,
)
from octomate.tentacles.mcp import OAuthMcpTentacle

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)
LarkApprovalActionValueAdapter = TypeAdapter(LarkApprovalActionValue)
LarkQuestionActionValueAdapter = TypeAdapter(LarkQuestionActionValue)
LarkQuestionFormValueAdapter = TypeAdapter(LarkQuestionFormValue)


def log_card_action_result[T](channel_id: str, task: asyncio.Task[T]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "Channel %s: failed to handle Lark card action",
            channel_id,
            exc_info=(type(error), error, error.__traceback__),
        )


class LarkTentacle(
    ChannelTentacle[P2ImMessageReceiveV1, LarkOutboundMessage], OAuthMcpTentacle
):
    """Lark channel: a long-lived WebSocket event connection translated into core
    events, with optional Feishu MCP tools authorized by each user."""

    brand_color: ClassVar[Style | None] = Style(color="#666D82", bold=True)
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(
        sub_thread=True, direct_message=True
    )
    label: str = "Lark / Feishu"
    upstream: str = "https://mcp.feishu.cn/mcp"
    instructions: str = (
        "Feishu tools act as the person who connected their account, from any "
        "channel. Access is limited to that person's permissions and the tools "
        "enabled by the deployment. Use gateway_send to speak in a conversation."
    )
    config: LarkChannelConfig
    feelers: Feelers
    ink: LarkInk
    chromo: LarkChromo

    ws_client: LarkWebSocketClient
    ping_task: asyncio.Task[None] | None

    @property
    def log_names(self) -> tuple[str, ...]:
        return (*super().log_names, "Lark")  # the lark-oapi SDK logger

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: LarkChannelConfig,
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=LarkInk(config.app_id, config.app_secret),
            chromo=LarkChromo(),
            config=config,
        )

        event_handler = (
            lark_oapi.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.sense)
            .register_p2_card_action_trigger(self.on_card_action)
            .build()
        )
        self.ws_client = LarkWebSocketClient(
            self.ink.app_id,
            self.ink.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark_oapi.LogLevel.INFO,
        )
        self.ping_task = None
        markdown_feeler = LarkMarkdownFeeler(ink=self.ink, chromo=self.chromo)
        approvals = LarkApprovalFeeler(self.ink)
        ask_questions = LarkAskQuestionFeeler(self.ink)
        oauth = LarkOAuthFeeler(self.ink)
        self.feelers = Feelers(
            markdown=markdown_feeler,
            artifacts=LarkArtifactFeeler(
                artifacts=self.octomate.artifacts,
                users=self.octomate.users,
                ink=self.ink,
            ),
            timeline=LarkTimelineFeeler(
                ink=self.ink,
                chromo=self.chromo,
                stream_config=self.config.stream,
                ask_questions=ask_questions,
                approvals=approvals,
                oauth=oauth,
                deferred_actions=self.octomate.deferred_actions,
            ),
            segments=DefaultSegmentsFeeler(ink=self.ink, chromo=self.chromo),
            approvals=approvals,
            ask_questions=ask_questions,
            oauth=oauth,
        )
        if config.oauth is not None:
            octomate.oauth.register(
                LarkOAuthConnector(
                    id=id,
                    mcp_url=AnyHttpUrl(self.upstream),
                    httpx_client_factory=octomate.oauth.httpx_client_factory,
                    flows=[
                        AuthorizationCodeFlow(
                            tokens=OAuthTokenExchange(
                                authorization_endpoint=AnyHttpUrl(
                                    "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
                                ),
                                token_endpoint=AnyHttpUrl(
                                    # Feishu documents v2 for PKCE with this authorization endpoint.
                                    "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
                                ),
                                client=OAuthClientInformationFull(
                                    client_id=config.app_id,
                                    client_secret=config.app_secret.get_secret_value(),
                                    token_endpoint_auth_method="client_secret_post",
                                ),
                                scopes=config.oauth.scopes,
                                httpx_client_factory=octomate.oauth.httpx_client_factory,
                            ),
                            authorization_lifetime=octomate.oauth.authorization_lifetime,
                        )
                    ],
                    callback_transport=DirectHttpOAuthCallbackTransport(
                        config.oauth.callback_base_uri
                    ),
                )
            )

    @property
    def serving(self) -> bool:
        return self.config.mcp

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        # lark-oapi attaches its own stdout handler to the "Lark" logger and also
        # propagates, so its records would print twice; drop that handler so they
        # surface once through the host's console handler in our format.
        logging.getLogger("Lark").handlers.clear()
        logger.info("Channel %s: starting Lark WebSocket client", self.id)
        # lark-oapi exposes a blocking public start(), but its WebSocket client
        # is async internally. Run those internals on Octomate's event loop so
        # message callbacks can schedule ingest directly.
        ws_mod.loop = asyncio.get_running_loop()
        await self.ws_client._connect()  # type: ignore[attr-defined]
        self.ping_task = asyncio.create_task(self.ws_client._ping_loop())  # type: ignore[attr-defined]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.ping_task is not None:
            ping_task = self.ping_task
            self.ping_task = None
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        try:
            await self.ws_client.close()
        finally:
            await super().__aexit__(*exc)

    def is_shared(self, address: ChannelAddress) -> bool:
        """A one-to-one chat is keyed on the person's open id, `ou_…`, and a topic
        in one keeps it; a group's chat id is `oc_…`."""
        return not address.chat_id.startswith("ou_")

    async def start_sub_thread(
        self,
        address: ChannelAddress,
        hint_text: str,
    ) -> ChannelAddress:
        message_id = await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            self.chromo.outbound_markdown(hint_text),
            channel_thread_id=address.chat_id or address.user_id,
        )
        if message_id is None:
            return address
        return replace(address, chat_type="thread", channel_thread_id=message_id)

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
            logger.warning(
                "Channel %s: unrecognized card action %r",
                self.id,
                value.get("action"),
            )
            return P2CardActionTriggerResponse({})
        logger.info("Channel %s: card action %s", self.id, action.value)
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
                {"toast": {"type": "info", "content": "Response received"}}
            )

        if action == LarkCardAction.ASK_QUESTION_SUBMIT:
            try:
                action_value = LarkQuestionActionValueAdapter.validate_python(value)
                form_value = LarkQuestionFormValueAdapter.validate_python(
                    callback_action.form_value or {}
                )
            except ValidationError:
                return P2CardActionTriggerResponse({})
            question_actions = action_value["questions"]
            answers = collect_answers(question_actions, form_value)
            task = asyncio.create_task(
                self.octomate.kick(
                    DeferredActionBatchResponse(
                        batch_id=action_value["batch_id"],
                        responder_id=responder_id,
                        answers={
                            question.id: answers.get(question.id, "")
                            for question in question_actions
                        },
                    )
                )
            )
            task.add_done_callback(lambda task: log_card_action_result(self.id, task))
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "Response received"}}
            )

        return P2CardActionTriggerResponse({})
