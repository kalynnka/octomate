from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import cast

import discord
import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import OAuthDeviceAuthorizationEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredActionBatch,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.schemas.triage import SummonDecision
from octomate.tentacles.discord.feelers.actions import DiscordComponentRouter
from octomate.tentacles.discord.feelers.approvals import (
    APPROVAL_CUSTOM_ID_TEMPLATE,
    DiscordApprovalButton,
    DiscordApprovalFeeler,
    approval_content,
    approval_resolution_content,
)
from octomate.tentacles.discord.feelers.oauth import DiscordOAuthFeeler
from octomate.tentacles.discord.feelers.questions import (
    QUESTION_ANSWER_CUSTOM_ID_TEMPLATE,
    QUESTION_CHOICE_CUSTOM_ID_TEMPLATE,
    QUESTION_NAV_CUSTOM_ID_TEMPLATE,
    DiscordAskQuestionFeeler,
    DiscordQuestionAnswerButton,
    DiscordQuestionChoiceButton,
    DiscordQuestionModal,
    DiscordQuestionNavButton,
    question_summary_content,
)
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from tests.channels.discord.fakes import a_user
from tests.support.managers import a_thread


@dataclass(frozen=True)
class DiscordUISend:
    chat_id: str
    chat_type: str
    message: DiscordOutboundMessage
    channel_thread_id: str


class RecordingDiscordUIInk(DiscordInk):
    def __init__(self) -> None:
        self.sent: list[DiscordUISend] = []
        self.opened_dms: list[str] = []

    async def open_dm(self, user_id: str, opener: str | None = None) -> str:
        assert opener is None
        self.opened_dms.append(user_id)
        return "900"

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[DiscordOutboundMessage],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str:
        assert len(messages) == 1
        assert reply_to is None
        assert reply_in_thread is False
        self.sent.append(
            DiscordUISend(
                chat_id=chat_id,
                chat_type=chat_type,
                message=messages[0],
                channel_thread_id=channel_thread_id,
            )
        )
        return str(800 + len(self.sent))


@dataclass(frozen=True)
class InteractionEdit:
    content: str | None
    view: discord.ui.View | discord.ui.LayoutView | None
    allowed_mentions: discord.AllowedMentions


class FakeInteractionResponse:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.modal: discord.ui.Modal | None = None

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        assert ephemeral is False
        assert thinking is False
        self.events.append("defer")

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.events.append("send_modal")
        self.modal = modal


class FakeInteractionFollowup:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        self.events.append("followup")
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(
        self,
        client: discord.Client,
        events: list[str],
        *,
        user_id: int = 100,
    ) -> None:
        self.client = client
        self.user = a_user(user_id)
        self.response = FakeInteractionResponse(events)
        self.followup = FakeInteractionFollowup(events)
        self.events = events
        self.edits: list[InteractionEdit] = []

    async def edit_original_response(
        self,
        *,
        content: str | None,
        view: discord.ui.View | discord.ui.LayoutView | None,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self.events.append("edit")
        self.edits.append(
            InteractionEdit(
                content=content,
                view=view,
                allowed_mentions=allowed_mentions,
            )
        )


def layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


class ResolvingOctomate(Octomate):
    kicks: list[DeferredActionBatchResponse]
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.kicks = []
        self.events = events

    async def kick(self, signal: DeferredActionBatchResponse) -> None:
        self.events.append("kick")
        self.kicks.append(signal)
        await self.deferred_actions.resolve_batch(signal)


def discord_address(*, shared: bool = False) -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="discord",
        chat_type="thread",
        chat_id="400",
        user_id="100",
        channel_thread_id="500",
        shared=shared,
    )


async def create_batch(
    *,
    questions: list[QuestionRequest] | None = None,
    approval: bool = False,
) -> DeferredActionBatch:
    address = discord_address()
    conversation = await ConversationManager().ensure(
        await a_thread(f"discord-{uuid.uuid4()}"),
        agent_tentacle_id="inkling",
    )
    requests = DeferredToolRequests(
        calls=(
            [
                ToolCallPart(
                    tool_name="ask_questions",
                    args={"questions": questions},
                    tool_call_id="call_questions",
                )
            ]
            if questions
            else []
        ),
        approvals=(
            [
                ToolCallPart(
                    tool_name="shell",
                    args={"cmd": "git status"},
                    tool_call_id="call_approval",
                )
            ]
            if approval
            else []
        ),
    )
    return await DeferredActionManager().create_batch(
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=SummonDecision(
            action="summon",
            agent_id="inkling",
            model="test",
            reason="needs input",
            hint="needs input",
            summon="needs input",
        ),
        requests=requests,
    )


def approval() -> DeferredApproval:
    return DeferredApproval(
        id=uuid7(),
        batch_id=uuid7(),
        tool_name="shell",
        tool_call_id="call_approval",
        args=ApprovalRequest(tool_name="shell", args={"cmd": "git status"}),
    )


def question(
    text: str,
    *,
    choices: list[str] | None = None,
) -> DeferredQuestion:
    args: QuestionRequest = {"question": text}
    if choices is not None:
        args["choices"] = choices
    return DeferredQuestion(
        id=uuid7(),
        batch_id=uuid7(),
        tool_name="ask_questions",
        tool_call_id="call_questions",
        args=args,
    )


async def test_discord_feelers_render_one_question_navigator_per_batch() -> None:
    ink = RecordingDiscordUIInk()
    approval_action = approval()
    choice_question = question("Environment?", choices=["prod", "stage", "dev"])
    text_question = question("Why?")

    approval_ids = await DiscordApprovalFeeler(ink).present(
        discord_address(),
        [approval_action],
    )
    question_ids = await DiscordAskQuestionFeeler(ink).present(
        discord_address(),
        [choice_question, text_question],
    )

    assert approval_ids == {approval_action.id: "801"}
    assert question_ids == {choice_question.id: "802", text_question.id: "802"}
    assert len(ink.sent) == 2
    assert all(call.channel_thread_id == "500" for call in ink.sent)
    approval_message = ink.sent[0].message
    assert "git status" in approval_message.content
    assert approval_message.view is not None
    approval_buttons = [
        item
        for item in approval_message.view.children
        if isinstance(item, DiscordApprovalButton)
    ]
    assert len(approval_buttons) == 2
    assert all(len(item.custom_id) <= 100 for item in approval_buttons)
    assert all("git status" not in item.custom_id for item in approval_buttons)

    question_message = ink.sent[1].message
    choice_view = question_message.view
    assert isinstance(choice_view, discord.ui.LayoutView)
    [container] = choice_view.children
    assert isinstance(container, discord.ui.Container)
    text_display, choices_row, separator, navigation_row = container.children
    assert isinstance(text_display, discord.ui.TextDisplay)
    assert isinstance(choices_row, discord.ui.ActionRow)
    assert isinstance(separator, discord.ui.Separator)
    assert isinstance(navigation_row, discord.ui.ActionRow)
    content = layout_text(choice_view)
    assert "Question 1 of 2" in content
    assert "Environment?" in content
    assert "Why?" not in content
    first_choice, second_choice, third_choice, other_button, next_button = (
        item
        for item in choice_view.walk_children()
        if isinstance(
            item,
            DiscordQuestionChoiceButton
            | DiscordQuestionAnswerButton
            | DiscordQuestionNavButton,
        )
    )
    assert isinstance(first_choice, DiscordQuestionChoiceButton)
    assert isinstance(second_choice, DiscordQuestionChoiceButton)
    assert isinstance(third_choice, DiscordQuestionChoiceButton)
    assert [
        first_choice.item.label,
        second_choice.item.label,
        third_choice.item.label,
    ] == [
        "prod",
        "stage",
        "dev",
    ]
    assert isinstance(other_button, DiscordQuestionAnswerButton)
    assert other_button.item.label == "Other…"
    assert isinstance(next_button, DiscordQuestionNavButton)
    assert next_button.operation == "next"
    assert next_button.item.disabled


def test_discord_action_content_stays_within_message_limit() -> None:
    approval_action = DeferredApproval(
        id=uuid7(),
        batch_id=uuid7(),
        tool_name="shell",
        tool_call_id="call_approval",
        args=ApprovalRequest(
            tool_name="t" * 500,
            title="h" * 500,
            description="d" * 500,
            args={"payload": "x" * 3000},
        ),
    )

    rendered_approval = approval_content(approval_action)
    long_answer = question("q" * 500)
    rendered_answer = question_summary_content(
        [long_answer],
        {long_answer.id: "a" * 4000},
        responder_id="100",
    )

    assert len(rendered_approval) <= 2000
    assert "… (truncated)" in rendered_approval
    assert (
        len(
            approval_resolution_content(
                approval_action,
                approved=True,
                responder_id="100",
            )
        )
        <= 2000
    )
    assert len(rendered_answer) == 2000
    assert rendered_answer.endswith("Answered by <@100>")


async def test_discord_oauth_moves_shared_authorization_to_a_link_button_dm() -> None:
    ink = RecordingDiscordUIInk()
    event = OAuthDeviceAuthorizationEvent(
        connector_id="github",
        label="GitHub",
        authorization_uri="https://github.com/login/device",
        user_code="ABCD-EFGH",
    )

    message_id = await DiscordOAuthFeeler(ink).present(
        discord_address(shared=True),
        event,
    )

    assert message_id == "801"
    assert ink.opened_dms == ["100"]
    [sent] = ink.sent
    assert (sent.chat_id, sent.chat_type, sent.channel_thread_id) == (
        "900",
        "dm",
        "900",
    )
    assert "ABCD-EFGH" in sent.message.content
    assert sent.message.view is not None
    [button] = sent.message.view.children
    assert isinstance(button, discord.ui.Button)
    assert button.style is discord.ButtonStyle.link
    assert button.url == event.authorization_uri
    assert button.custom_id is None


async def test_approval_callback_reloads_after_restart_and_resolves_once(
    in_memory_engine: AsyncEngine,
) -> None:
    batch = await create_batch(approval=True)
    [approval_action] = batch.approvals
    events: list[str] = []
    octomate = ResolvingOctomate(events)
    client = discord.Client(intents=discord.Intents.none())
    DiscordComponentRouter(octomate).bind(client)
    interaction = FakeInteraction(client, events)
    custom_id = DiscordApprovalButton(
        batch.id,
        approval_action.id,
        True,
    ).custom_id
    match = APPROVAL_CUSTOM_ID_TEMPLATE.fullmatch(custom_id)
    assert match is not None
    restored = await DiscordApprovalButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", interaction),
        discord.ui.Button(label="Approve", custom_id=custom_id),
        match,
    )

    await restored.callback(cast("discord.Interaction[discord.Client]", interaction))

    assert events == ["defer", "edit", "kick"]
    assert octomate.kicks == [
        DeferredActionBatchResponse(
            batch_id=batch.id,
            responder_id="100",
            approvals={approval_action.id: True},
        )
    ]
    reloaded = await octomate.deferred_actions.get_batch(batch.id)
    [resolved] = reloaded.approvals
    assert (resolved.status, resolved.responder_id) == ("approved", "100")
    assert reloaded.status == "resolved"
    [edit] = interaction.edits
    assert edit.content is not None
    assert "Approved by <@100>" in edit.content
    assert edit.view is not None
    assert all(
        isinstance(item, DiscordApprovalButton) and item.item.disabled
        for item in edit.view.children
    )

    duplicate = FakeInteraction(client, events)
    await restored.callback(cast("discord.Interaction[discord.Client]", duplicate))

    assert events[-2:] == ["defer", "followup"]
    assert duplicate.followup.messages == [("This approval was already handled.", True)]
    assert len(octomate.kicks) == 1
    await client.close()


async def test_approval_callback_persists_before_settling_the_message(
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = await create_batch(approval=True)
    [approval_action] = batch.approvals
    events: list[str] = []
    octomate = ResolvingOctomate(events)
    client = discord.Client(intents=discord.Intents.none())
    DiscordComponentRouter(octomate).bind(client)
    interaction = FakeInteraction(client, events)

    async def fail_resolution(
        response: DeferredActionBatchResponse,
    ) -> DeferredActionBatch:
        events.append("resolve")
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(octomate.deferred_actions, "resolve_batch", fail_resolution)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await DiscordApprovalButton(batch.id, approval_action.id, True).callback(
            cast("discord.Interaction[discord.Client]", interaction)
        )

    assert events == ["defer", "resolve"]
    assert interaction.edits == []
    await client.close()


async def test_unrelated_batches_do_not_wait_for_an_agent_run(
    in_memory_engine: AsyncEngine,
) -> None:
    first_batch = await create_batch(approval=True)
    second_batch = await create_batch(approval=True)
    [first_action] = first_batch.approvals
    [second_action] = second_batch.approvals
    events: list[str] = []
    first_kick_started = asyncio.Event()
    release_first_kick = asyncio.Event()

    class SlowFirstKickOctomate(ResolvingOctomate):
        async def kick(self, signal: DeferredActionBatchResponse) -> None:
            self.events.append(f"kick:{signal.batch_id}")
            self.kicks.append(signal)
            if signal.batch_id == first_batch.id:
                first_kick_started.set()
                await release_first_kick.wait()

    octomate = SlowFirstKickOctomate(events)
    client = discord.Client(intents=discord.Intents.none())
    DiscordComponentRouter(octomate).bind(client)
    first_interaction = FakeInteraction(client, events)
    second_interaction = FakeInteraction(client, events)

    first_callback = asyncio.create_task(
        DiscordApprovalButton(first_batch.id, first_action.id, True).callback(
            cast("discord.Interaction[discord.Client]", first_interaction)
        )
    )
    await first_kick_started.wait()
    await asyncio.wait_for(
        DiscordApprovalButton(second_batch.id, second_action.id, True).callback(
            cast("discord.Interaction[discord.Client]", second_interaction)
        ),
        timeout=1,
    )
    release_first_kick.set()
    await first_callback

    assert len(second_interaction.edits) == 1
    assert len(octomate.kicks) == 2
    await client.close()


async def test_approval_callback_rejects_unknown_and_mismatched_actions(
    in_memory_engine: AsyncEngine,
) -> None:
    first = await create_batch(approval=True)
    second = await create_batch(approval=True)
    [first_action] = first.approvals
    [second_action] = second.approvals
    events: list[str] = []
    octomate = ResolvingOctomate(events)
    client = discord.Client(intents=discord.Intents.none())
    DiscordComponentRouter(octomate).bind(client)

    for batch_id, action_id, expected in [
        (uuid.uuid4(), first_action.id, "no longer available"),
        (first.id, second_action.id, "does not belong"),
    ]:
        interaction = FakeInteraction(client, events)
        await DiscordApprovalButton(batch_id, action_id, False).callback(
            cast("discord.Interaction[discord.Client]", interaction)
        )
        assert expected in interaction.followup.messages[0][0]
        assert interaction.followup.messages[0][1] is True

    assert octomate.kicks == []
    await client.close()


async def test_question_navigator_preserves_drafts_and_submits_the_batch(
    in_memory_engine: AsyncEngine,
) -> None:
    exact_choice = "a" * 120
    exact_text = "  keep this spacing  "
    batch = await create_batch(
        questions=[
            {"question": "Environment?", "choices": ["prod", exact_choice]},
            {"question": "Why?"},
        ]
    )
    first, second = sorted(batch.questions)
    events: list[str] = []
    octomate = ResolvingOctomate(events)
    client = discord.Client(intents=discord.Intents.none())
    router = DiscordComponentRouter(octomate)
    router.bind(client)

    original_choice = DiscordQuestionChoiceButton(
        batch.id,
        first.id,
        1,
        exact_choice,
    )
    choice_match = QUESTION_CHOICE_CUSTOM_ID_TEMPLATE.fullmatch(
        original_choice.custom_id
    )
    assert choice_match is not None
    choice_interaction = FakeInteraction(client, events)
    restored_choice = await DiscordQuestionChoiceButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", choice_interaction),
        discord.ui.Button(
            label=original_choice.item.label,
            custom_id=original_choice.custom_id,
        ),
        choice_match,
    )
    await restored_choice.callback(
        cast("discord.Interaction[discord.Client]", choice_interaction)
    )

    assert events == ["defer", "edit"]
    assert octomate.kicks == []
    [second_page] = choice_interaction.edits
    assert isinstance(second_page.view, discord.ui.LayoutView)
    second_page_content = layout_text(second_page.view)
    assert "Question 2 of 2" in second_page_content
    assert "Why?" in second_page_content
    blocked_submit = next(
        item
        for item in second_page.view.walk_children()
        if isinstance(item, DiscordQuestionNavButton) and item.operation == "submit"
    )
    assert blocked_submit.item.disabled
    write_answer = next(
        item
        for item in second_page.view.walk_children()
        if isinstance(item, DiscordQuestionAnswerButton)
    )
    assert write_answer.item.label == "Write an answer…"
    drafted = await octomate.deferred_actions.get_batch(batch.id)
    first_draft, second_draft = sorted(drafted.questions)
    assert (first_draft.result, first_draft.status) == (None, "pending")
    assert (second_draft.result, second_draft.status) == (None, "pending")
    assert drafted.status == "pending"
    assert router.question_answers == {batch.id: {first.id: exact_choice}}
    assert original_choice.item.label is not None
    assert len(original_choice.item.label) == 80

    blocked_submit_match = QUESTION_NAV_CUSTOM_ID_TEMPLATE.fullmatch(
        blocked_submit.custom_id
    )
    assert blocked_submit_match is not None
    blocked_submit_interaction = FakeInteraction(client, events)
    restored_blocked_submit = await DiscordQuestionNavButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", blocked_submit_interaction),
        discord.ui.Button(label="Submit", custom_id=blocked_submit.custom_id),
        blocked_submit_match,
    )
    await restored_blocked_submit.callback(
        cast("discord.Interaction[discord.Client]", blocked_submit_interaction)
    )
    assert blocked_submit_interaction.followup.messages == [
        ("Answer every question before submitting.", True)
    ]

    original_answer = DiscordQuestionAnswerButton(batch.id, second.id)
    answer_match = QUESTION_ANSWER_CUSTOM_ID_TEMPLATE.fullmatch(
        original_answer.custom_id
    )
    assert answer_match is not None
    answer_interaction = FakeInteraction(client, events)
    restored_answer = await DiscordQuestionAnswerButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", answer_interaction),
        discord.ui.Button(label="Answer", custom_id=original_answer.custom_id),
        answer_match,
    )
    await restored_answer.callback(
        cast("discord.Interaction[discord.Client]", answer_interaction)
    )
    modal = answer_interaction.response.modal
    assert isinstance(modal, DiscordQuestionModal)
    question_display = next(
        item for item in modal.children if isinstance(item, discord.ui.TextDisplay)
    )
    assert question_display.content == "**Why?**"
    modal.answer._value = exact_text
    modal_interaction = FakeInteraction(client, events, user_id=101)
    await modal.on_submit(
        cast("discord.Interaction[discord.Client]", modal_interaction)
    )

    modal_view = modal_interaction.edits[0].view
    assert isinstance(modal_view, discord.ui.LayoutView)
    modal_content = layout_text(modal_view)
    assert "Question 2 of 2" in modal_content
    assert f"**Answer:** {exact_text}" in modal_content
    assert octomate.kicks == []

    second_page_view = modal_view
    previous = next(
        item
        for item in second_page_view.walk_children()
        if isinstance(item, DiscordQuestionNavButton) and item.operation == "back"
    )
    previous_match = QUESTION_NAV_CUSTOM_ID_TEMPLATE.fullmatch(previous.custom_id)
    assert previous_match is not None
    previous_interaction = FakeInteraction(client, events)
    restored_previous = await DiscordQuestionNavButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", previous_interaction),
        discord.ui.Button(label="Previous", custom_id=previous.custom_id),
        previous_match,
    )
    await restored_previous.callback(
        cast("discord.Interaction[discord.Client]", previous_interaction)
    )

    [first_page] = previous_interaction.edits
    assert isinstance(first_page.view, discord.ui.LayoutView)
    first_page_content = layout_text(first_page.view)
    assert "Question 1 of 2" in first_page_content
    assert f"**Answer:** {exact_choice}" in first_page_content
    selected_choice = next(
        item
        for item in first_page.view.walk_children()
        if isinstance(item, DiscordQuestionChoiceButton) and item.choice_index == 1
    )
    assert selected_choice.item.style is discord.ButtonStyle.primary

    submit = next(
        item
        for item in second_page_view.walk_children()
        if isinstance(item, DiscordQuestionNavButton) and item.operation == "submit"
    )
    assert not submit.item.disabled
    submit_match = QUESTION_NAV_CUSTOM_ID_TEMPLATE.fullmatch(submit.custom_id)
    assert submit_match is not None
    submit_interaction = FakeInteraction(client, events, user_id=101)
    restored_submit = await DiscordQuestionNavButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", submit_interaction),
        discord.ui.Button(label="Submit", custom_id=submit.custom_id),
        submit_match,
    )
    await restored_submit.callback(
        cast("discord.Interaction[discord.Client]", submit_interaction)
    )

    [summary] = submit_interaction.edits
    assert isinstance(summary.view, discord.ui.LayoutView)
    summary_content = layout_text(summary.view)
    assert "Answers submitted" in summary_content
    assert exact_choice in summary_content
    assert exact_text in summary_content
    assert "Answered by <@101>" in summary_content
    assert [kick.answers for kick in octomate.kicks] == [
        {first.id: exact_choice, second.id: exact_text},
    ]
    resolved = await octomate.deferred_actions.get_batch(batch.id)
    assert [action.status for action in sorted(resolved.questions)] == [
        "answered",
        "answered",
    ]
    assert resolved.status == "resolved"
    assert router.question_answers == {}

    stale = FakeInteraction(client, events)
    await restored_choice.callback(cast("discord.Interaction[discord.Client]", stale))
    assert stale.followup.messages == [
        ("These questions were already submitted.", True)
    ]
    assert len(octomate.kicks) == 1
    await client.close()
