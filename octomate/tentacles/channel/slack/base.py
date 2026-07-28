from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import TypeAdapter, ValidationError
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncSay

from octomate.config import SlackChannelConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.base import sqlalchemy_materia
from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.oauth import OAuthPending
from octomate.tentacles.channel.base import (
    ChannelSurfaces,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.output import DefaultSegmentsFeeler
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.approvals import (
    SlackApprovalFeeler,
    approval_blocks,
    approval_submitted_blocks,
    approval_title,
)
from octomate.tentacles.channel.slack.feelers.oauth import (
    SlackOAuthFeeler,
    authorization_blocks,
    authorization_connected_blocks,
    authorization_failed_blocks,
)
from octomate.tentacles.channel.slack.feelers.output import SlackTimelineFeeler
from octomate.tentacles.channel.slack.feelers.questions import (
    SlackAskQuestionFeeler,
    SlackQuestionActionValueAdapter,
    ask_question_blocks,
    collect_current_answer,
    question_title,
    submitted_blocks,
)
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import (
    SlackApprovalActionBody,
    SlackAssistantThreadEvent,
    SlackMessageEvent,
    SlackOAuthActionBody,
    SlackOutboundMessage,
    SlackQuestionActionBody,
)

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)
SlackApprovalActionBodyAdapter = TypeAdapter(SlackApprovalActionBody)
SlackOAuthActionBodyAdapter = TypeAdapter(SlackOAuthActionBody)
SlackQuestionActionBodyAdapter = TypeAdapter(SlackQuestionActionBody)

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


class SlackTentacle(ChannelTentacle[SlackMessageEvent, SlackOutboundMessage]):
    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(
        sub_thread=True, direct_message=True
    )
    feelers: Feelers
    ink: SlackInk
    chromo: SlackChromo

    @property
    def log_names(self) -> tuple[str, ...]:
        # The Slack SDKs log socket/connection activity under their own loggers.
        return (*super().log_names, "slack_bolt", "slack_sdk")

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: SlackChannelConfig,
    ) -> None:
        ink = SlackInk(config.bot_token)
        chromo = SlackChromo()
        super().__init__(
            id=id,
            octomate=octomate,
            ink=ink,
            chromo=chromo,
            config=config,
        )
        self.app_id = config.app_id
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
        self.app.action(SlackBlockAction.ASK_QUESTION_BACK.value)(self.on_question_nav)
        self.app.action(SlackBlockAction.ASK_QUESTION_NEXT.value)(self.on_question_nav)
        self.app.action(SlackBlockAction.ASK_QUESTION_SUBMIT.value)(
            self.on_question_nav
        )
        self.app.action(SlackBlockAction.ASK_QUESTION_CHOICE.value)(
            self.on_question_nav
        )
        self.app.action(SlackBlockAction.OAUTH_CONFIRM.value)(self.on_oauth_action)
        # A url button still posts an interaction; ack it so Slack does not mark the
        # message as failed.
        self.app.action(SlackBlockAction.OAUTH_OPEN.value)(self.on_link_action)
        self.app_token = config.app_token
        self.handler: AsyncSocketModeHandler | None = None
        # In-flight inbound turns. `on_message` runs the turn off the socket
        # listener (see there); the set keeps a strong reference so the task is
        # not garbage-collected mid-run.
        self.ingest_tasks: set[asyncio.Task[None]] = set()
        markdown_feeler = self.feelers.markdown
        approvals = SlackApprovalFeeler(self.ink)
        ask_questions = SlackAskQuestionFeeler(self.ink)
        oauth = SlackOAuthFeeler(self.ink)
        self.feelers = Feelers(
            markdown=markdown_feeler,
            timeline=SlackTimelineFeeler(
                ink=self.ink,
                chromo=self.chromo,
                ask_questions=ask_questions,
                approvals=approvals,
                oauth=oauth,
                deferred_actions=self.octomate.deferred_actions,
                stream_config=config.stream,
            ),
            segments=DefaultSegmentsFeeler(ink=self.ink, chromo=self.chromo),
            approvals=approvals,
            ask_questions=ask_questions,
            oauth=oauth,
        )

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        logger.info("Channel %s: starting Slack Socket Mode client", self.id)
        self.handler = AsyncSocketModeHandler(
            self.app,
            self.app_token.get_secret_value(),
        )
        # start_async parks forever after connecting; the host still needs to
        # enter the remaining channels in its lifespan stack.
        await self.handler.connect_async()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.handler:
            await self.handler.close_async()
            self.handler = None

    async def on_message(self, event: SlackMessageEvent, say: AsyncSay) -> None:
        subtype = event.get("subtype")
        if subtype in IGNORED_SUBTYPES:
            return
        if (
            event.get("bot_id")
            or event.get("user") == self.self_profile.channel_user_id
        ):
            return
        # Run the turn off the socket listener so bolt acks the envelope right
        # away. Awaiting `ingest` here would hold the ack until the whole run
        # finishes — and a turn parked on an approval would miss Slack's ~3s ack
        # window, making Slack re-deliver the event (duplicate runs). `ingest`
        # logs its own exceptions, so the task needs no result handling.
        task = asyncio.create_task(self.ingest(event))
        self.ingest_tasks.add(task)
        task.add_done_callback(self.ingest_tasks.discard)

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

    async def on_link_action(self, ack) -> None:
        """A url button opens the link client-side; Slack still expects the ack."""
        await ack()

    async def on_oauth_action(self, ack, body: SlackOAuthActionBody) -> None:
        """Finish the connection this message was posted for, then rewrite it.

        Scoped to whoever pressed the button: `complete_latest` looks for a pending
        authorization owned by *their* profile, so a bystander in a shared channel
        completes nothing — there is no operation of theirs to find, and they are
        told so rather than handed someone else's connection.
        """
        await ack()
        try:
            action_body = SlackOAuthActionBodyAdapter.validate_python(body)
        except ValidationError as error:
            logger.warning(
                "Channel %s: ignored invalid Slack oauth action: %s",
                self.id,
                error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                ),
            )
            return
        value = action_body["actions"][0]["value"]
        label = value["label"]
        profile = await self.octomate.users.ensure_profile(
            self.id,
            await self.get_user_profile(action_body["user"]["id"]),
        )
        try:
            result = await self.octomate.oauth.complete_latest(
                profile,
                value["connector_id"],
            )
        except ValueError as error:
            text = f"Could not connect {label}"
            blocks = authorization_failed_blocks(label=label, detail=str(error))
        else:
            if isinstance(result, OAuthPending):
                text = f"Connect {label}"
                blocks = authorization_blocks(
                    OAuthAuthorizationEvent(
                        connector_id=value["connector_id"],
                        label=label,
                        verification_uri=value["verification_uri"],
                        user_code=value["user_code"],
                    ),
                    note=(
                        f"{label} has not accepted the code yet — finish there, "
                        f"then press again in about {result.retry_after_seconds}s."
                    ),
                )
            else:
                text = f"{label} connected"
                blocks = authorization_connected_blocks(
                    label=label,
                    account_label=result.account_label,
                )
        await self.ink.update_message(
            action_body["channel"]["id"],
            action_body["message"]["ts"],
            text=text,
            blocks=blocks,
        )

    async def on_approval_action(self, ack, body: SlackApprovalActionBody) -> None:
        await ack()
        try:
            action_body = SlackApprovalActionBodyAdapter.validate_python(body)
        except ValidationError as error:
            logger.warning(
                "Channel %s: ignored invalid Slack approval action: %s",
                self.id,
                error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                ),
            )
            return
        action = action_body["actions"][0]
        action_value = action_body["actions"][0]["value"]
        responder_id = action_body["user"]["id"]
        approved = action["action_id"] == SlackBlockAction.APPROVAL_APPROVE.value
        channel = action_body["channel"]["id"]
        message_ts = action_body["message"]["ts"]
        actions = action_value["approvals"]
        page = max(0, min(action_value["page"], len(actions) - 1))
        approved_action = actions[page]
        decisions = dict(action_value["decisions"])
        decisions[approved_action.id] = approved
        next_page = next(
            (
                index
                for index, candidate in enumerate(actions)
                if candidate.id not in decisions
            ),
            None,
        )
        await self.ink.update_message(
            channel,
            message_ts,
            text=(
                approval_title(actions)
                if next_page is not None
                else "Approvals handled"
            ),
            blocks=(
                approval_blocks(actions, page=next_page, decisions=decisions)
                if next_page is not None
                else approval_submitted_blocks(
                    actions,
                    decisions,
                    responder_id=responder_id,
                )
            ),
        )
        await self.octomate.kick(
            DeferredActionBatchResponse(
                batch_id=action_value["batch_id"],
                responder_id=responder_id,
                approvals={approved_action.id: approved},
            )
        )

    async def on_question_nav(self, ack, body: SlackQuestionActionBody) -> None:
        await ack()
        try:
            action_body = SlackQuestionActionBodyAdapter.validate_python(body)
        except ValidationError as error:
            logger.warning(
                "Channel %s: ignored invalid Slack question action: %s",
                self.id,
                error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                ),
            )
            return
        action = action_body["actions"][0]
        action_value = action.get("value")
        if action_value is None:
            for block in reversed(action_body["message"].get("blocks", [])):
                if block.get("type") != "actions":
                    continue
                elements = block.get("elements", [])
                if not isinstance(elements, list):
                    continue
                for raw_element in elements:
                    if not isinstance(raw_element, dict):
                        continue
                    if raw_element.get("action_id") not in {
                        SlackBlockAction.ASK_QUESTION_BACK.value,
                        SlackBlockAction.ASK_QUESTION_NEXT.value,
                        SlackBlockAction.ASK_QUESTION_SUBMIT.value,
                    }:
                        continue
                    value = raw_element.get("value")
                    if not isinstance(value, str):
                        continue
                    try:
                        action_value = SlackQuestionActionValueAdapter.validate_json(
                            value
                        )
                    except ValidationError:
                        continue
                    break
                if action_value is not None:
                    break
        if action_value is None:
            logger.warning(
                "Channel %s: ignored Slack question action without navigation state",
                self.id,
            )
            return
        actions = action_value["questions"]
        page = action_value["page"]
        answers = dict(action_value["answers"])
        action_id = action["action_id"]
        answers = collect_current_answer(
            action_body["state"],
            actions,
            page,
            answers,
            prefer_choice=action_id == SlackBlockAction.ASK_QUESTION_CHOICE.value,
        )
        if action_id == SlackBlockAction.ASK_QUESTION_BACK.value:
            page -= 1
        elif action_id in {
            SlackBlockAction.ASK_QUESTION_NEXT.value,
            SlackBlockAction.ASK_QUESTION_CHOICE.value,
        }:
            page += 1
        else:
            await self.ink.update_message(
                action_body["channel"]["id"],
                action_body["message"]["ts"],
                text="Answers submitted",
                blocks=submitted_blocks(actions, answers),
            )
            await self.octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=action_value["batch_id"],
                    responder_id=action_body["user"]["id"],
                    answers={
                        action.id: str(answers.get(action.id, "")) for action in actions
                    },
                )
            )
            return
        await self.ink.update_message(
            action_body["channel"]["id"],
            action_body["message"]["ts"],
            text=question_title(actions),
            blocks=ask_question_blocks(
                actions,
                page=page,
                answers=answers,
            ),
        )

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

        address = ChannelAddress(
            channel_tentacle_id=self.id,
            chat_type="private",
            chat_id=channel_id,
            user_id=user_id,
            thread_id=thread_ts,
        )
        with sqlalchemy_materia():
            # Pre-create the thread that owns this assistant chat's conversations;
            # the first message would otherwise create it on ingest.
            await self.octomate.thread_manager.ensure(address)
        logger.info("Channel %s: ensured Slack assistant thread %s", self.id, address)

    async def open_dm(self, user_id: str) -> ChannelAddress | None:
        """The user's DM with the bot, opened through `conversations.open`."""
        if not user_id:
            return None
        channel_id = await self.ink.open_dm(user_id)
        if channel_id is None:
            return None
        return ChannelAddress(
            channel_tentacle_id=self.id,
            chat_type="private",
            chat_id=channel_id,
            user_id=user_id,
            thread_id="",
        )

    async def start_sub_thread(
        self,
        address: ChannelAddress,
        hint_text: str,
    ) -> ChannelAddress:
        message_id = await self.ink.send_message(
            address.chat_id or address.user_id,
            address.chat_type,
            [SlackOutboundMessage(text=hint_text, markdown_text=hint_text)],
            None,
        )
        return replace(address, thread_id=message_id or address.thread_id)
