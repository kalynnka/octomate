from __future__ import annotations

import re
import uuid
from typing import Self

import discord

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion
from octomate.telemetry import channel_logfire
from octomate.tentacles.discord.feelers.actions import (
    DiscordActionUnavailable,
    DiscordChoiceAnswer,
    DiscordComponentRouter,
)
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.deferred import QuestionFeeler, question_text
from octomate.tentacles.feelers.output import IMMessageID

QUESTION_SELECT_CUSTOM_ID_TEMPLATE = re.compile(
    r"om:q:s:(?P<batch>[0-9a-f]{32}):(?P<action>[0-9a-f]{32})"
)
QUESTION_ANSWER_CUSTOM_ID_TEMPLATE = re.compile(
    r"om:q:a:(?P<batch>[0-9a-f]{32}):(?P<action>[0-9a-f]{32})"
)


class DiscordQuestionSelect(
    discord.ui.DynamicItem[discord.ui.Select[discord.ui.View]],
    template=QUESTION_SELECT_CUSTOM_ID_TEMPLATE,
):
    def __init__(
        self,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        choices: list[str] | None = None,
        *,
        item: discord.ui.Select[discord.ui.View] | None = None,
        disabled: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.action_id = action_id
        if item is None:
            item = discord.ui.Select(
                custom_id=f"om:q:s:{batch_id.hex}:{action_id.hex}",
                placeholder="Choose an answer",
                options=[
                    discord.SelectOption(
                        label=choice[:100] or "Empty answer",
                        value=str(index),
                    )
                    for index, choice in enumerate(choices or [])
                ],
                disabled=disabled,
            )
        super().__init__(item)

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[discord.Client],
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> Self:
        if not isinstance(item, discord.ui.Select):
            raise TypeError("Discord question choice control is not a select")
        return cls(
            uuid.UUID(hex=match["batch"]),
            uuid.UUID(hex=match["action"]),
            item=item,
        )

    async def callback(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        await interaction.response.defer()
        if len(self.item.values) != 1 or not self.item.values[0].isdigit():
            await interaction.followup.send(
                "This answer choice is invalid.",
                ephemeral=True,
            )
            return
        router = DiscordComponentRouter.for_client(interaction.client)

        async def settle_message(action: DeferredQuestion, answer: str) -> None:
            await interaction.edit_original_response(
                content=question_resolution_content(
                    action,
                    answer=answer,
                    responder_id=str(interaction.user.id),
                ),
                view=question_view(action, disabled=True),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        try:
            await router.resolve_question(
                batch_id=self.batch_id,
                action_id=self.action_id,
                responder_id=str(interaction.user.id),
                answer=DiscordChoiceAnswer(index=int(self.item.values[0])),
                settle_message=settle_message,
            )
        except DiscordActionUnavailable as error:
            await interaction.followup.send(str(error), ephemeral=True)


class DiscordQuestionAnswerButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=QUESTION_ANSWER_CUSTOM_ID_TEMPLATE,
):
    def __init__(
        self,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        *,
        disabled: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.action_id = action_id
        super().__init__(
            discord.ui.Button(
                label="Answer",
                style=discord.ButtonStyle.primary,
                custom_id=f"om:q:a:{batch_id.hex}:{action_id.hex}",
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[discord.Client],
        item: discord.ui.Item[discord.ui.View],
        match: re.Match[str],
        /,
    ) -> Self:
        if not isinstance(item, discord.ui.Button):
            raise TypeError("Discord question answer control is not a button")
        return cls(
            uuid.UUID(hex=match["batch"]),
            uuid.UUID(hex=match["action"]),
            disabled=item.disabled,
        )

    async def callback(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        await interaction.response.send_modal(
            DiscordQuestionModal(self.batch_id, self.action_id)
        )


class DiscordQuestionModal(discord.ui.Modal):
    def __init__(self, batch_id: uuid.UUID, action_id: uuid.UUID) -> None:
        super().__init__(
            title="Answer question",
            custom_id=f"om:q:m:{batch_id.hex}:{action_id.hex}",
        )
        self.batch_id = batch_id
        self.action_id = action_id
        self.answer = discord.ui.TextInput(
            custom_id="answer",
            style=discord.TextStyle.paragraph,
            placeholder="Type your answer",
            max_length=4000,
        )
        self.add_item(discord.ui.Label(text="Your answer", component=self.answer))

    async def on_submit(
        self,
        interaction: discord.Interaction[discord.Client],
        /,
    ) -> None:
        await interaction.response.defer()
        router = DiscordComponentRouter.for_client(interaction.client)

        async def settle_message(action: DeferredQuestion, answer: str) -> None:
            await interaction.edit_original_response(
                content=question_resolution_content(
                    action,
                    answer=answer,
                    responder_id=str(interaction.user.id),
                ),
                view=question_view(action, disabled=True),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        try:
            await router.resolve_question(
                batch_id=self.batch_id,
                action_id=self.action_id,
                responder_id=str(interaction.user.id),
                answer=self.answer.value,
                settle_message=settle_message,
            )
        except DiscordActionUnavailable as error:
            await interaction.followup.send(str(error), ephemeral=True)


class DiscordAskQuestionFeeler(QuestionFeeler):
    def __init__(self, ink: DiscordInk) -> None:
        self.ink = ink

    @channel_logfire.instrument("discord.ask_questions.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[uuid.UUID, IMMessageID | None]:
        message_ids: dict[uuid.UUID, IMMessageID | None] = {}
        chat_id = address.chat_id or address.user_id
        for action in actions:
            message_ids[action.id] = await self.ink.send_message(
                chat_id,
                address.chat_type,
                [
                    DiscordOutboundMessage(
                        content=question_content(action),
                        view=question_view(action),
                    )
                ],
                channel_thread_id=address.channel_thread_id or chat_id,
            )
        return message_ids


def question_content(action: DeferredQuestion) -> str:
    hint = action.args.get("hint") or ""
    body = f"**Question**\n{question_text(action)}"
    if hint:
        body = f"{body}\n\n*Hint: {hint}*"
    return body[:2000]


def question_view(
    action: DeferredQuestion,
    *,
    disabled: bool = False,
) -> discord.ui.View:
    if action.batch_id is None:
        raise ValueError("Discord question controls require a batch id")
    view = discord.ui.View(timeout=None)
    choices = list(action.args.get("choices") or [])
    if choices:
        view.add_item(
            DiscordQuestionSelect(
                action.batch_id,
                action.id,
                choices,
                disabled=disabled,
            )
        )
    view.add_item(
        DiscordQuestionAnswerButton(
            action.batch_id,
            action.id,
            disabled=disabled,
        )
    )
    return view


def question_resolution_content(
    action: DeferredQuestion,
    *,
    answer: str,
    responder_id: str,
) -> str:
    heading = f"**{question_text(action)[:200]}**\n"
    footer = f"\n\nAnswered by <@{responder_id}>"
    answer_limit = 2000 - len(heading) - len(footer)
    rendered_answer = answer
    if len(answer) > answer_limit:
        rendered_answer = f"{answer[: answer_limit - 1]}…"
    return f"{heading}{rendered_answer}{footer}"
