from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import cast

import pytest
from openai_codex.models import Notification
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.deferred import Interjections
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.config.agents import CodexConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex.base import CodexBridgeContext
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.types.json import JsonObject


async def never() -> AsyncIterator[Notification]:
    """A turn that says nothing while it waits on its answer."""
    await asyncio.Event().wait()
    yield cast(Notification, None)


@pytest.mark.parametrize("answer", ["Accept", "Decline", "Cancel", None])
async def test_mcp_consent_rides_the_turn_and_returns_the_selected_answer(
    in_memory_engine: AsyncEngine, answer: str | None
) -> None:
    octomate = Octomate()
    tentacle = CodexTentacle("codex", octomate, config=CodexConfig(approval_timeout=5))
    octomate.connect(tentacle)
    octomate.connect(
        TrunklineTentacle(
            "trunkline",
            octomate,
            config=TrunklineChannelConfig(agents=["codex"]),
        )
    )
    address = ChannelAddress(
        channel_tentacle_id="trunkline",
        chat_type="thread",
        chat_id="dev",
        channel_thread_id="approval-test",
        user_id="dev",
    )
    thread = await octomate.thread_manager.ensure(address)
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    params: JsonObject = {
        "threadId": "codex-thread",
        "turnId": "turn",
        "itemId": "mcp-call",
        "isBlocking": True,
        "questions": [
            {
                "id": "mcp-approval",
                "header": "Permission",
                "question": "Allow octomate.history_search to search for peaches?",
                "options": [
                    {"label": "Accept", "description": "Run the search."},
                    {"label": "Decline", "description": "Do not run the search."},
                    {"label": "Cancel", "description": "Cancel this request."},
                ],
            },
            {
                "id": "follow-up",
                "header": "Next step",
                "question": "Which message should be read next?",
                "options": None,
            },
        ],
    }
    interjections = Interjections[Notification]()
    tentacle.bridge_contexts[conversation.id] = CodexBridgeContext(
        loop=asyncio.get_running_loop(),
        conversation=conversation,
        session_allowed=set(),
        suspender=ReflexSuspender(
            channel=octomate.channels["trunkline"],
            action_manager=octomate.deferred_actions,
            conversation_manager=octomate.conversations,
            agent_tentacle_id="codex",
            run_name="react",
            source_address=address,
            target_address=address,
            target_mode="sub",
            decision=None,
            thread_id=thread.id,
            emit_on_stream=True,
        ),
        interjections=interjections,
    )
    turn = interjections.around(never())
    async with aclosing(turn):
        # The SDK invokes its handler on a plain transport thread.
        task = asyncio.get_running_loop().run_in_executor(
            None,
            tentacle.handle_sdk_request,
            conversation.id,
            "item/tool/requestUserInput",
            params,
        )
        try:
            card = await asyncio.wait_for(anext(turn), timeout=1)
            assert isinstance(card, ActionBatchEvent)
            question, follow_up = sorted(card.questions)
            assert question.args.get("choices") == ["Accept", "Decline", "Cancel"]
            assert "peaches" in question.args["question"]
            assert follow_up.args.get("choices") is None
            assert not task.done()
            batch_id = question.batch_id
            assert batch_id is not None
            if answer is None:
                assert await task == {"answers": {}}
                batch = await octomate.deferred_actions.get_batch(batch_id)
                assert batch.status == "expired"
                assert tentacle.pending == {}
                return
            await octomate.kick(
                DeferredActionBatchResponse(
                    batch_id=batch_id,
                    answers={follow_up.id: "Read the next reply", question.id: answer},
                )
            )
            assert await task == {
                "answers": {
                    "mcp-approval": {"answers": [answer]},
                    "follow-up": {"answers": ["Read the next reply"]},
                }
            }
            batch = await octomate.deferred_actions.get_batch(batch_id)
            assert [row.result for row in sorted(batch.questions)] == [
                answer,
                "Read the next reply",
            ]
            assert conversation.allowed_tools == []
        finally:
            await asyncio.wait_for(task, timeout=6)
