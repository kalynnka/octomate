from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar

from pydantic import TypeAdapter, ValidationError
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp, AsyncSay

from octomate.config import SlackChannelConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.slack.chromo import SlackChromo
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.approvals import (
    SlackApprovalFeeler,
    approval_blocks,
    approval_submitted_blocks,
    approval_title,
)
from octomate.tentacles.channel.slack.feelers.questions import (
    SlackAskQuestionFeeler,
    SlackQuestionActionValueAdapter,
    ask_question_blocks,
    collect_current_answer,
    question_title,
    submitted_blocks,
)
from octomate.tentacles.channel.slack.feelers.output import (
    SlackEventStreamFeeler,
    SlackMarkdownStreamFeeler,
    SlackTimelineFeeler,
)
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import (
    SlackApprovalActionBody,
    SlackAssistantThreadEvent,
    SlackMessageEvent,
    SlackOutboundMessage,
    SlackQuestionActionBody,
)

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)
SlackApprovalActionBodyAdapter = TypeAdapter(SlackApprovalActionBody)
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
    feelers: Feelers[ChannelOutput]
    ink: SlackInk
    chromo: SlackChromo

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
        self.app_token = config.app_token
        self.handler: AsyncSocketModeHandler | None = None
        markdown_feeler = self.feelers.markdown
        self.feelers = Feelers(
            markdown=markdown_feeler,
            markdown_stream=SlackMarkdownStreamFeeler[ChannelOutput](
                ink=self.ink,
                chromo=self.chromo,
                stream_config=self.config.stream,
                markdown_feeler=markdown_feeler,
                channel_id=self.id,
            ),
            event_stream=SlackEventStreamFeeler[ChannelOutput](
                ink=self.ink,
                chromo=self.chromo,
                markdown_feeler=markdown_feeler,
                channel_id=self.id,
            ),
            timeline=SlackTimelineFeeler(ink=self.ink, chromo=self.chromo),
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
