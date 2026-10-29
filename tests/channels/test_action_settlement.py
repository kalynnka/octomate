"""Answers from another surface redraw the persisted IM cards through the nerve."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.events import (
    ActionBatchAnsweredEvent,
    ActionBatchEvent,
    RunEndedEvent,
    RunStartedEvent,
)
from octomate.managers.conversation import ConversationManager
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredActionBatch
from octomate.tentacles.discord.feelers.actions import DiscordComponentRouter
from octomate.tentacles.discord.feelers.approvals import DiscordApprovalFeeler
from octomate.tentacles.discord.feelers.questions import DiscordAskQuestionFeeler
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.feelers.output import DefaultTimelineFeeler
from octomate.tentacles.lark.feelers.approvals import LarkApprovalFeeler
from octomate.tentacles.lark.feelers.questions import LarkAskQuestionFeeler
from octomate.tentacles.lark.ink import LarkInk
from octomate.tentacles.slack.feelers.approvals import SlackApprovalFeeler
from octomate.tentacles.slack.feelers.questions import SlackAskQuestionFeeler
from octomate.tentacles.slack.ink import SlackInk
from octomate.types.json import JsonObject
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_thread
from tests.support.nerve import drawing

Mode = Literal["outside", "streaming", "quiet"]


@pytest.fixture(params=["slack", "discord", "lark", "plaintext"])
async def surface(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[FakeChannelTentacle, AsyncMock]]:
    channel = FakeChannelTentacle(id=request.param)
    edits = AsyncMock()
    sends = AsyncMock(side_effect=["701", "702", "703"])
    async with httpx.AsyncClient() as http:
        if channel.id == "slack":
            ink = SlackInk(SecretStr("unused"))
            monkeypatch.setattr(ink, "send_message", sends)
            monkeypatch.setattr(ink, "update_message", edits)
            channel.feelers.approvals = SlackApprovalFeeler(ink)
            channel.feelers.ask_questions = SlackAskQuestionFeeler(ink)
        elif channel.id == "discord":
            ink = DiscordInk(discord.Client(intents=discord.Intents.none()), http=http)
            monkeypatch.setattr(ink, "send_message", sends)
            monkeypatch.setattr(ink, "edit_message", edits)
            channel.feelers.approvals = DiscordApprovalFeeler(ink)
            channel.feelers.ask_questions = DiscordAskQuestionFeeler(ink)
        elif channel.id == "lark":
            ink = LarkInk("unused", SecretStr("unused"))
            monkeypatch.setattr(ink, "send_message", sends)
            monkeypatch.setattr(ink, "patch_card", edits)
            channel.feelers.approvals = LarkApprovalFeeler(ink)
            channel.feelers.ask_questions = LarkAskQuestionFeeler(ink)
        channel.feelers.timeline = DefaultTimelineFeeler(
            ink=channel.ink,
            chromo=channel.chromo,
            approvals=channel.feelers.approvals,
            ask_questions=channel.feelers.ask_questions,
            oauth=channel.feelers.oauth,
            deferred_actions=channel.octomate.deferred_actions,
        )
        yield channel, edits


@pytest.fixture
async def batch(
    surface: tuple[FakeChannelTentacle, AsyncMock], in_memory_engine
) -> DeferredActionBatch:
    channel, _ = surface
    address = ChannelAddress(channel.id, "thread", "400", "100", "500")
    conversation = await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="inkling"
    )
    manager = channel.octomate.deferred_actions
    batch = await manager.create_batch(
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=None,
        response_mode="resume",
        requests=DeferredToolRequests(
            approvals=[
                ToolCallPart("shell", {"cmd": "pwd"}, "approve-one"),
                ToolCallPart("shell", {"cmd": "ls"}, "approve-two"),
            ],
            calls=[
                ToolCallPart(
                    "ask_questions",
                    {
                        "questions": [
                            {
                                "question": "Region?",
                                "choices": ["us", "eu"],
                                "multi_select": True,
                            },
                            {"question": "Anything else?"},
                        ]
                    },
                    "questions",
                )
            ],
        ),
    )
    await channel.feelers.present_actions(
        address, ActionBatchEvent.from_batch(batch), action_manager=manager
    )
    return await manager.get_batch(batch.id)


async def publish(
    channel: FakeChannelTentacle,
    address: ChannelAddress,
    event: ActionBatchAnsweredEvent,
    mode: Mode,
) -> None:
    channel.config.stream.enabled = mode == "streaming"
    topic = ChannelTopic(channel.id)
    async with drawing(channel):
        if mode != "outside":
            channel.octomate.nerve.deliver(
                topic, Impulse(RunStartedEvent(address=address), address)
            )
        channel.octomate.nerve.deliver(topic, Impulse(event, address))
        if mode != "outside":
            channel.octomate.nerve.deliver(topic, Impulse(RunEndedEvent(), address))


def rendered(channel: FakeChannelTentacle, edits: AsyncMock) -> list[str]:
    payloads: list[str] = []
    for call in edits.await_args_list:
        if channel.id == "discord":
            payloads.append(
                json.dumps([call.args[2], call.kwargs["view"].to_components()])
            )
        elif channel.id == "slack":
            payloads.append(json.dumps(call.kwargs["blocks"]))
        else:
            payloads.append(call.args[1])
    return payloads


@pytest.mark.parametrize("mode", ["outside", "streaming", "quiet"])
async def test_answers_settle_cards_in_and_out_of_a_run(
    surface: tuple[FakeChannelTentacle, AsyncMock],
    batch: DeferredActionBatch,
    mode: Mode,
) -> None:
    channel, edits = surface
    first, second = batch.approvals
    region, other = sorted(batch.questions)
    response = DeferredActionBatchResponse(
        batch_id=batch.id,
        approvals={first.id: True, second.id: False},
        answers={region.id: ["us", "eu"], other.id: ""},
    )
    await channel.octomate.deferred_actions.resolve_batch(response)
    event = ActionBatchAnsweredEvent.of(response)
    await publish(channel, batch.target_address, event, mode)

    if channel.id == "plaintext":
        edits.assert_not_awaited()
        assert len(channel.sent) == 4
        return
    payloads = rendered(channel, edits)
    assert len(payloads) == (2 if channel.id == "slack" else 3)
    text = " ".join(payloads)
    assert "Approved" in text
    assert "Denied" in text
    assert "Region?" in text
    assert "us, eu" in text
    assert "No answer provided" in text
    if channel.id == "discord":
        assert all(call.args[0] == "500" for call in edits.await_args_list)
        for call in edits.await_args_list[:2]:
            assert all(item.item.disabled for item in call.kwargs["view"].children)
        assert "custom_id" not in payloads[-1]
    else:
        assert '"button"' not in text
        if channel.id == "slack":
            assert all(call.args[0] == "400" for call in edits.await_args_list)
    saved_ids = {
        action.platform_message_id for action in [*batch.approvals, *batch.questions]
    }
    assert {
        call.args[0 if channel.id == "lark" else 1] for call in edits.await_args_list
    } == saved_ids

    # Redelivery and a stale decision must display the accepted, persisted result.
    edits.reset_mock()
    event.approvals[first.id] = False
    await publish(channel, batch.target_address, event, mode)
    assert rendered(channel, edits) == payloads


async def test_partial_answers_keep_the_remaining_controls(
    surface: tuple[FakeChannelTentacle, AsyncMock], batch: DeferredActionBatch
) -> None:
    channel, edits = surface
    first, second = batch.approvals
    region, other = sorted(batch.questions)
    response = DeferredActionBatchResponse(
        batch_id=batch.id, approvals={first.id: True}, answers={region.id: ["eu"]}
    )
    await channel.octomate.deferred_actions.resolve_batch(response)
    await publish(
        channel, batch.target_address, ActionBatchAnsweredEvent.of(response), "outside"
    )
    if channel.id == "plaintext":
        edits.assert_not_awaited()
        return
    payloads = rendered(channel, edits)
    assert len(payloads) == 2
    assert "Region?" in payloads[1]
    assert "eu" in payloads[1]
    assert "Anything else?" in payloads[1]
    if channel.id == "slack":
        blocks = edits.await_args_list[0].kwargs["blocks"]
        value = json.loads(blocks[-1]["elements"][0]["value"])
        assert value["page"] == 1
        assert value["decisions"] == {str(first.id): True}
        assert '"button"' in payloads[1]
    elif channel.id == "discord":
        assert str(other.id.hex) in payloads[1]
        assert region.id.hex not in payloads[1]
        actions, _ = await DiscordComponentRouter(channel.octomate).load_questions(
            batch.id
        )
        assert [action.id for action in actions] == [other.id]
    else:
        assert '"tag":"form"' in payloads[1]

    edits.reset_mock()
    response = DeferredActionBatchResponse(
        batch_id=batch.id, approvals={second.id: False}, answers={other.id: "done"}
    )
    await channel.octomate.deferred_actions.resolve_batch(response)
    await publish(
        channel, batch.target_address, ActionBatchAnsweredEvent.of(response), "quiet"
    )
    payloads = rendered(channel, edits)
    assert "Denied" in payloads[0]
    if channel.id == "slack":
        assert "Approved" in payloads[0]
    assert "eu" in payloads[1]
    assert "done" in payloads[1]
    assert "custom_id" not in payloads[1]
    assert '"button"' not in payloads[1]


async def test_unpresented_actions_do_not_edit_a_message(
    surface: tuple[FakeChannelTentacle, AsyncMock], batch: DeferredActionBatch
) -> None:
    channel, edits = surface
    manager = channel.octomate.deferred_actions
    unseen = await manager.create_batch(
        conversation=await ConversationManager().get(batch.conversation_id),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=batch.source_address,
        target_address=batch.target_address,
        target_mode="main",
        decision=None,
        response_mode="resume",
        requests=batch.requests,
    )
    response = DeferredActionBatchResponse(
        batch_id=unseen.id,
        approvals={action.id: True for action in unseen.approvals},
        answers={action.id: "elsewhere" for action in unseen.questions},
    )
    await manager.resolve_batch(response)
    await publish(
        channel,
        batch.target_address,
        ActionBatchAnsweredEvent.of(response),
        "streaming",
    )
    edits.assert_not_awaited()


@pytest.mark.parametrize("surface", ["slack", "discord", "lark"], indirect=True)
@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_settlement_updates_distinct_cards_concurrently_and_waits_for_them(
    surface: tuple[FakeChannelTentacle, AsyncMock],
    batch: DeferredActionBatch,
    kind: Literal["approval", "question"],
) -> None:
    channel, edits = surface
    manager = channel.octomate.deferred_actions
    actions = batch.approvals if kind == "approval" else batch.questions
    for index, action in enumerate(actions):
        await manager.mark_action_presented(action.id, str(800 + index))
    response = DeferredActionBatchResponse(
        batch_id=batch.id,
        approvals={action.id: True for action in batch.approvals},
        answers={action.id: "done" for action in batch.questions},
    )
    await manager.resolve_batch(response)
    started = asyncio.Event()
    release = asyncio.Event()
    entered = 0
    completed = 0

    async def edit(
        *args: str | None,
        **kwargs: str | list[JsonObject] | discord.ui.View | discord.ui.LayoutView,
    ) -> None:
        nonlocal entered, completed
        entered += 1
        if entered == 2:
            started.set()
        await release.wait()
        completed += 1

    edits.side_effect = edit
    feeler = (
        channel.feelers.approvals
        if kind == "approval"
        else channel.feelers.ask_questions
    )
    async with asyncio.TaskGroup() as tasks:
        settlement = tasks.create_task(
            feeler.settle(
                batch.target_address,
                ActionBatchAnsweredEvent.of(response),
                action_manager=manager,
            )
        )
        async with asyncio.timeout(2):
            await started.wait()
        assert not settlement.done()
        release.set()
    assert completed == 2
