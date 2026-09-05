from __future__ import annotations

import re
import uuid
from typing import Literal, Self

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

QUESTION_CHOICE_CUSTOM_ID_TEMPLATE = re.compile(
    r"om:q:c:(?P<batch>[0-9a-f]{32}):(?P<action>[0-9a-f]{32}):(?P<choice>\d+)"
)
QUESTION_ANSWER_CUSTOM_ID_TEMPLATE = re.compile(
    r"om:q:a:(?P<batch>[0-9a-f]{32}):(?P<action>[0-9a-f]{32})"
)
QUESTION_NAV_CUSTOM_ID_TEMPLATE = re.compile(
    r"om:q:n:(?P<batch>[0-9a-f]{32}):(?P<page>\d+):(?P<operation>[bns])"
)
QuestionNavOperation = Literal["back", "next", "submit"]


class DiscordQuestionChoiceButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=QUESTION_CHOICE_CUSTOM_ID_TEMPLATE,
):
    def __init__(
        self,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        choice_index: int,
        label: str = "",
        *,
        selected: bool = False,
        disabled: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.action_id = action_id
        self.choice_index = choice_index
        super().__init__(
            discord.ui.Button(
                label=label[:80] or "Empty answer",
                style=(
                    discord.ButtonStyle.primary
                    if selected
                    else discord.ButtonStyle.secondary
                ),
                custom_id=f"om:q:c:{batch_id.hex}:{action_id.hex}:{choice_index}",
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
            raise TypeError("Discord question choice control is not a button")
        return cls(
            uuid.UUID(hex=match["batch"]),
            uuid.UUID(hex=match["action"]),
            int(match["choice"]),
            item.label or "",
            selected=item.style is discord.ButtonStyle.primary,
            disabled=item.disabled,
        )

    async def callback(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        await interaction.response.defer()
        router = DiscordComponentRouter.for_client(interaction.client)
        try:
            actions, answers = await router.save_question_answer(
                batch_id=self.batch_id,
                action_id=self.action_id,
                answer=DiscordChoiceAnswer(index=self.choice_index),
            )
            page = next(
                index
                for index, action in enumerate(actions)
                if action.id == self.action_id
            )
            await edit_question_page(interaction, actions, answers, page + 1)
        except DiscordActionUnavailable as error:
            await interaction.followup.send(str(error), ephemeral=True)


class DiscordQuestionAnswerButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=QUESTION_ANSWER_CUSTOM_ID_TEMPLATE,
):
    def __init__(
        self,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        *,
        label: str = "Write an answer…",
        selected: bool = False,
        disabled: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.action_id = action_id
        super().__init__(
            discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if selected
                    else discord.ButtonStyle.secondary
                ),
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
            label=item.label or "Write an answer…",
            selected=item.style is discord.ButtonStyle.primary,
            disabled=item.disabled,
        )

    async def callback(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        router = DiscordComponentRouter.for_client(interaction.client)
        try:
            actions, answers = await router.load_questions(self.batch_id)
        except DiscordActionUnavailable as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        action = next(
            (action for action in actions if action.id == self.action_id),
            None,
        )
        if action is None:
            await interaction.response.send_message(
                "This question does not belong to this request.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            DiscordQuestionModal(
                self.batch_id,
                self.action_id,
                question=question_text(action),
                default=answers.get(self.action_id),
            )
        )


class DiscordQuestionNavButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=QUESTION_NAV_CUSTOM_ID_TEMPLATE,
):
    def __init__(
        self,
        batch_id: uuid.UUID,
        page: int,
        operation: QuestionNavOperation,
        *,
        disabled: bool = False,
    ) -> None:
        self.batch_id = batch_id
        self.page = page
        self.operation = operation
        code = {"back": "b", "next": "n", "submit": "s"}[operation]
        super().__init__(
            discord.ui.Button(
                label={"back": "Previous", "next": "Next", "submit": "Submit"}[
                    operation
                ],
                style=(
                    discord.ButtonStyle.secondary
                    if operation == "back"
                    else discord.ButtonStyle.primary
                ),
                custom_id=f"om:q:n:{batch_id.hex}:{page}:{code}",
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
            raise TypeError("Discord question navigation control is not a button")
        match match["operation"]:
            case "b":
                operation: QuestionNavOperation = "back"
            case "n":
                operation = "next"
            case _:
                operation = "submit"
        return cls(
            uuid.UUID(hex=match["batch"]),
            int(match["page"]),
            operation,
            disabled=item.disabled,
        )

    async def callback(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        await interaction.response.defer()
        router = DiscordComponentRouter.for_client(interaction.client)
        try:
            if self.operation == "submit":

                async def settle_message(
                    actions: list[DeferredQuestion],
                    answers: dict[uuid.UUID, str],
                ) -> None:
                    await interaction.edit_original_response(
                        content=None,
                        view=question_summary_view(
                            question_summary_content(
                                actions,
                                answers,
                                responder_id=str(interaction.user.id),
                            )
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

                await router.submit_questions(
                    batch_id=self.batch_id,
                    responder_id=str(interaction.user.id),
                    settle_message=settle_message,
                )
                return
            actions, answers = await router.load_questions(self.batch_id)
            page = self.page - 1 if self.operation == "back" else self.page + 1
            await edit_question_page(interaction, actions, answers, page)
        except DiscordActionUnavailable as error:
            await interaction.followup.send(str(error), ephemeral=True)


class DiscordQuestionModal(discord.ui.Modal):
    def __init__(
        self,
        batch_id: uuid.UUID,
        action_id: uuid.UUID,
        *,
        question: str,
        default: str | None = None,
    ) -> None:
        super().__init__(
            title="Answer question",
            custom_id=f"om:q:m:{batch_id.hex}:{action_id.hex}",
        )
        self.batch_id = batch_id
        self.action_id = action_id
        self.add_item(discord.ui.TextDisplay(f"**{question[:3996]}**"))
        self.answer = discord.ui.TextInput(
            custom_id="answer",
            style=discord.TextStyle.paragraph,
            placeholder="Type your answer",
            default=default,
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
        try:
            actions, answers = await router.save_question_answer(
                batch_id=self.batch_id,
                action_id=self.action_id,
                answer=self.answer.value,
            )
            page = next(
                index
                for index, action in enumerate(actions)
                if action.id == self.action_id
            )
            await edit_question_page(interaction, actions, answers, page)
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
        if not actions:
            return {}
        actions = sorted(actions)
        chat_id = address.chat_id or address.user_id
        message_id = await self.ink.send_message(
            chat_id,
            address.chat_type,
            [
                DiscordOutboundMessage(
                    view=question_view(actions),
                )
            ],
            channel_thread_id=address.channel_thread_id or chat_id,
        )
        return {action.id: message_id for action in actions}


async def edit_question_page(
    interaction: discord.Interaction[discord.Client],
    actions: list[DeferredQuestion],
    answers: dict[uuid.UUID, str],
    page: int,
) -> None:
    await interaction.edit_original_response(
        content=None,
        view=question_view(actions, answers, page=page),
        allowed_mentions=discord.AllowedMentions.none(),
    )


def question_page_content(
    actions: list[DeferredQuestion],
    answers: dict[uuid.UUID, str] | None = None,
    *,
    page: int = 0,
) -> str:
    answers = answers or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    heading = (
        f"**Question {page + 1} of {len(actions)}**"
        if len(actions) > 1
        else "**Question**"
    )
    body = f"{heading}\n{question_text(action)[:1000]}"
    if hint := action.args.get("hint"):
        body = f"{body}\n\n*Hint: {hint[:400]}*"
    if action.id in answers:
        answer_prefix = "\n\n**Answer:** "
        answer_limit = 2000 - len(body) - len(answer_prefix)
        answer = answers[action.id]
        if len(answer) > answer_limit:
            answer = f"{answer[: answer_limit - 1]}…"
        body = f"{body}{answer_prefix}{answer}"
    return body


def question_view(
    actions: list[DeferredQuestion],
    answers: dict[uuid.UUID, str] | None = None,
    *,
    page: int = 0,
) -> discord.ui.LayoutView:
    answers = answers or {}
    page = max(0, min(page, len(actions) - 1))
    action = actions[page]
    if action.batch_id is None:
        raise ValueError("Discord question controls require a batch id")
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        discord.ui.TextDisplay(question_page_content(actions, answers, page=page)),
        accent_color=discord.Color.blurple(),
    )
    choices_row = discord.ui.ActionRow()
    saved = answers.get(action.id, "")
    choices = list(action.args.get("choices") or [])
    for index, choice in enumerate(choices):
        choices_row.add_item(
            DiscordQuestionChoiceButton(
                action.batch_id,
                action.id,
                index,
                choice,
                selected=choice == saved,
            )
        )
    choices_row.add_item(
        DiscordQuestionAnswerButton(
            action.batch_id,
            action.id,
            label="Other…" if choices else "Write an answer…",
            selected=action.id in answers and saved not in choices,
        )
    )
    container.add_item(choices_row)
    container.add_item(discord.ui.Separator())
    navigation_row = discord.ui.ActionRow()
    if page > 0:
        navigation_row.add_item(DiscordQuestionNavButton(action.batch_id, page, "back"))
    navigation_row.add_item(
        DiscordQuestionNavButton(
            action.batch_id,
            page,
            "next" if page < len(actions) - 1 else "submit",
            disabled=action.id not in answers,
        )
    )
    container.add_item(navigation_row)
    view.add_item(container)
    return view


def question_summary_view(content: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(content),
            accent_color=discord.Color.blurple(),
        )
    )
    return view


def question_summary_content(
    actions: list[DeferredQuestion],
    answers: dict[uuid.UUID, str],
    *,
    responder_id: str,
) -> str:
    footer = f"\n\nAnswered by <@{responder_id}>"
    content = "**Answers submitted**"
    for index, action in enumerate(actions, start=1):
        answer = answers.get(action.id, "")
        entry = (
            f"\n\n**{index}. {question_text(action)[:200]}**\n"
            f"**Answer:** {answer or '[No answer provided]'}"
        )
        remaining = 2000 - len(content) - len(footer)
        if remaining <= 0:
            break
        if len(entry) > remaining:
            content = f"{content}{entry[: remaining - 1]}…"
            break
        content = f"{content}{entry}"
    return f"{content}{footer}"
