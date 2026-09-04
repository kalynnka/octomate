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
    QUESTION_SELECT_CUSTOM_ID_TEMPLATE,
    DiscordAskQuestionFeeler,
    DiscordQuestionAnswerButton,
    DiscordQuestionModal,
    DiscordQuestionSelect,
    question_resolution_content,
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
    content: str
    view: discord.ui.View
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
        content: str,
        view: discord.ui.View,
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


async def test_discord_feelers_render_one_persistent_message_per_action() -> None:
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
    assert question_ids == {choice_question.id: "802", text_question.id: "803"}
    assert len(ink.sent) == 3
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

    choice_view = ink.sent[1].message.view
    assert choice_view is not None
    choice_select = choice_view.children[0]
    assert isinstance(choice_select, DiscordQuestionSelect)
    assert [option.label for option in choice_select.item.options] == [
        "prod",
        "stage",
        "dev",
    ]
    assert isinstance(choice_view.children[1], DiscordQuestionAnswerButton)
    text_view = ink.sent[2].message.view
    assert text_view is not None
    assert len(text_view.children) == 1
    assert isinstance(text_view.children[0], DiscordQuestionAnswerButton)


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
    rendered_answer = question_resolution_content(
        question("q" * 500),
        answer="a" * 4000,
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
    assert "Approved by <@100>" in edit.content
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


async def test_question_select_and_modal_resolve_one_action_at_a_time(
    in_memory_engine: AsyncEngine,
) -> None:
    exact_choice = "a" * 120
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
    DiscordComponentRouter(octomate).bind(client)

    original_select = DiscordQuestionSelect(
        batch.id,
        first.id,
        first.args.get("choices") or [],
    )
    select_match = QUESTION_SELECT_CUSTOM_ID_TEMPLATE.fullmatch(
        original_select.custom_id
    )
    assert select_match is not None
    plain_select = discord.ui.Select(
        custom_id=original_select.custom_id,
        options=original_select.item.options,
    )
    select_interaction = FakeInteraction(client, events)
    restored_select = await DiscordQuestionSelect.from_custom_id(
        cast("discord.Interaction[discord.Client]", select_interaction),
        plain_select,
        select_match,
    )
    # Discord refreshes this public `values` result from the interaction payload
    # before invoking the callback; set its backing value to emulate that dispatch.
    restored_select.item._values = ["1"]

    await restored_select.callback(
        cast("discord.Interaction[discord.Client]", select_interaction)
    )

    assert events == ["defer", "edit"]
    assert octomate.kicks == []
    partially_resolved = await octomate.deferred_actions.get_batch(batch.id)
    first_reloaded, second_reloaded = sorted(partially_resolved.questions)
    assert first_reloaded.result == exact_choice
    assert first_reloaded.status == "answered"
    assert second_reloaded.status == "pending"
    assert partially_resolved.status == "pending"
    assert (
        len(
            cast(DiscordQuestionSelect, select_interaction.edits[0].view.children[0])
            .item.options[1]
            .label
        )
        == 100
    )

    original_button = DiscordQuestionAnswerButton(batch.id, second.id)
    button_match = QUESTION_ANSWER_CUSTOM_ID_TEMPLATE.fullmatch(
        original_button.custom_id
    )
    assert button_match is not None
    button_interaction = FakeInteraction(client, events)
    restored_button = await DiscordQuestionAnswerButton.from_custom_id(
        cast("discord.Interaction[discord.Client]", button_interaction),
        discord.ui.Button(label="Answer", custom_id=original_button.custom_id),
        button_match,
    )
    await restored_button.callback(
        cast("discord.Interaction[discord.Client]", button_interaction)
    )
    modal = button_interaction.response.modal
    assert isinstance(modal, DiscordQuestionModal)
    assert len(modal.custom_id) <= 100
    exact_text = "  keep this spacing  "
    modal.answer._value = exact_text
    modal_interaction = FakeInteraction(client, events, user_id=101)

    await modal.on_submit(
        cast("discord.Interaction[discord.Client]", modal_interaction)
    )

    assert events == [
        "defer",
        "edit",
        "send_modal",
        "defer",
        "edit",
        "kick",
    ]
    assert [kick.answers for kick in octomate.kicks] == [
        {first.id: exact_choice, second.id: exact_text},
    ]
    resolved = await octomate.deferred_actions.get_batch(batch.id)
    first_reloaded, second_reloaded = sorted(resolved.questions)
    assert first_reloaded.result == exact_choice
    assert second_reloaded.result == exact_text
    assert second_reloaded.responder_id == "101"
    assert resolved.status == "resolved"
    assert "keep this spacing" in modal_interaction.edits[0].content
    assert all(
        isinstance(item, DiscordQuestionSelect | DiscordQuestionAnswerButton)
        and item.item.disabled
        for item in modal_interaction.edits[0].view.children
    )

    stale = FakeInteraction(client, events)
    await restored_select.callback(cast("discord.Interaction[discord.Client]", stale))
    assert stale.followup.messages == [("This question was already answered.", True)]
    assert len(octomate.kicks) == 1
    await client.close()
