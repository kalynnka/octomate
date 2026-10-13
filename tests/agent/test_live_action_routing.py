"""A live action reply never starts another agent run."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import UUID7
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.deferred import Interjections
from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.config.agents import ClaudeCodeConfig, CodexConfig, DeepseekConfig
from octomate.reflex.graph import reflex_graph
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex.base import CodexBridgeContext
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.base import DeepseekBridgeContext
from octomate.types.deferred import DeferredResponseMode
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_thread

type LiveAgent = ClaudeCodeTentacle | CodexTentacle | DeepseekTentacle

ADDRESS = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)


@pytest.fixture(params=["claude", "codex", "deepseek"])
async def live_agent(
    request: pytest.FixtureRequest, in_memory_engine: AsyncEngine
) -> tuple[LiveAgent, Conversation]:
    app = Octomate(tentacles={"im": FakeChannelTentacle()})
    if request.param == "claude":
        agent = ClaudeCodeTentacle(
            "claude",
            app,
            config=ClaudeCodeConfig(),
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            files=app.files,
            conversations=app.conversations,
            deferred_actions=app.deferred_actions,
            workspaces=app.workspaces,
            users=app.users,
            bearers=app.bearers,
            mcp=app.mcp,
        )
    elif request.param == "codex":
        agent = CodexTentacle(
            "codex",
            app,
            config=CodexConfig(),
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            files=app.files,
            conversations=app.conversations,
            deferred_actions=app.deferred_actions,
            workspaces=app.workspaces,
            users=app.users,
            bearers=app.bearers,
            auth=app.auth,
            gateway_manager=app.gateway,
        )
    else:
        agent = DeepseekTentacle("deepseek", app, config=DeepseekConfig())
    app.connect(agent)
    conversation = await app.conversations.ensure(
        await a_thread(), agent_tentacle_id=agent.id
    )
    return agent, conversation


def a_suspender(agent: LiveAgent, conversation: Conversation) -> ReflexSuspender:
    """The graph's suspender for a live run."""
    app = agent.octomate
    return ReflexSuspender(
        channel=app.channels["im"],
        action_manager=app.deferred_actions,
        conversation_manager=app.conversations,
        agent_tentacle_id=agent.id,
        run_name="react",
        source_address=ADDRESS,
        target_address=ADDRESS,
        target_mode="main",
        decision=None,
        thread_id=conversation.thread_id,
    )


async def ask(
    agent: LiveAgent, conversation: Conversation
) -> tuple[ActionBatchEvent, DeferredActionBatchResponse | None]:
    requests = DeferredToolRequests(
        approvals=[ToolCallPart("shell", {"cmd": "pwd"}, "call-1")]
    )
    suspender = a_suspender(agent, conversation)
    if isinstance(agent, ClaudeCodeTentacle):
        return await agent._await_human(
            requests, suspender=suspender, interjections=Interjections()
        )
    if isinstance(agent, CodexTentacle):
        return await agent._await_human(
            context=CodexBridgeContext(
                conversation=conversation,
                session_allowed=set(),
                suspender=suspender,
                interjections=Interjections(),
            ),
            requests=requests,
        )
    return await agent._await_human(
        context=DeepseekBridgeContext(
            conversation=conversation,
            session_allowed=set(),
            interactive=True,
            suspender=suspender,
            frames=asyncio.Queue(),
        ),
        requests=requests,
    )


@pytest.fixture
def put_up(monkeypatch: pytest.MonkeyPatch) -> asyncio.Queue[ActionBatchEvent]:
    """Each batch a live run pauses on, once it is kept and its waiter is
    listening: the moment a channel drawing the run could put its cards up."""
    batches: asyncio.Queue[ActionBatchEvent] = asyncio.Queue()
    pause = ReflexSuspender.pause

    async def paused(
        suspender: ReflexSuspender, requests: DeferredToolRequests, *, batch_id: UUID7
    ) -> ActionBatchEvent:
        batch = await pause(suspender, requests, batch_id=batch_id)
        batches.put_nowait(batch)
        return batch

    monkeypatch.setattr(ReflexSuspender, "pause", paused)
    return batches


async def test_a_reply_reaches_the_live_request(
    live_agent: tuple[LiveAgent, Conversation],
    put_up: asyncio.Queue[ActionBatchEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, conversation = live_agent
    app = agent.octomate
    run = AsyncMock(side_effect=AssertionError("A live reply must not start a run"))
    monkeypatch.setattr(reflex_graph, "run", run)

    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
        asking = tasks.create_task(ask(agent, conversation))
        batch = await put_up.get()
        [approval] = batch.approvals
        await app.kick(
            DeferredActionBatchResponse(
                batch_id=UUID(batch.batch_id), approvals={approval.id: True}
            )
        )
    _, response = asking.result()

    assert response is not None
    stored = await app.deferred_actions.get_batch(UUID(batch.batch_id))
    assert stored.response_mode == "live"
    assert stored.status == "resolved"
    assert not agent.pendings
    run.assert_not_awaited()


async def test_pending_batches_accept_replies_in_reverse_order(
    live_agent: tuple[LiveAgent, Conversation],
    put_up: asyncio.Queue[ActionBatchEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, conversation = live_agent
    app = agent.octomate
    run = AsyncMock(side_effect=AssertionError("A live reply must not start a run"))
    monkeypatch.setattr(reflex_graph, "run", run)

    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
        calls = [tasks.create_task(ask(agent, conversation)) for _ in range(2)]
        batches = [await put_up.get(), await put_up.get()]
        for batch, approved in zip(reversed(batches), [True, False], strict=True):
            [approval] = batch.approvals
            await app.kick(
                DeferredActionBatchResponse(
                    batch_id=UUID(batch.batch_id), approvals={approval.id: approved}
                )
            )

    replies = {
        batch.batch_id: response
        for batch, response in [call.result() for call in calls]
    }
    for batch, approved in zip(batches, [False, True], strict=True):
        response = replies[batch.batch_id]
        assert response is not None
        assert response.approvals == {batch.approvals[0].id: approved}
    assert not agent.pendings
    run.assert_not_awaited()


async def test_a_pause_that_fails_removes_the_waiter(
    live_agent: tuple[LiveAgent, Conversation], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, conversation = live_agent
    monkeypatch.setattr(
        ReflexSuspender,
        "pause",
        AsyncMock(side_effect=RuntimeError("the batch could not be kept")),
    )
    with pytest.raises(RuntimeError, match="the batch could not be kept"):
        await ask(agent, conversation)
    assert not agent.pendings


@pytest.mark.parametrize("response_mode", ["live", "resume"])
async def test_missing_waiter_does_not_determine_response_routing(
    response_mode: DeferredResponseMode,
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Octomate()
    agent = CodexTentacle(
        "codex",
        app,
        config=CodexConfig(),
        commands=app.commands,
        projects=app.projects,
        threads=app.threads,
        files=app.files,
        conversations=app.conversations,
        deferred_actions=app.deferred_actions,
        workspaces=app.workspaces,
        users=app.users,
        bearers=app.bearers,
        auth=app.auth,
        gateway_manager=app.gateway,
    )
    app.connect(agent)
    conversation = await app.conversations.ensure(
        await a_thread(), agent_tentacle_id=agent.id
    )
    batch = await app.deferred_actions.create_batch(
        conversation=conversation,
        agent_tentacle_id=agent.id,
        response_mode=response_mode,
        run_name="react",
        source_address=ADDRESS,
        target_address=ADDRESS,
        target_mode="main",
        decision=None,
        requests=DeferredToolRequests(),
    )
    run = AsyncMock()
    monkeypatch.setattr(reflex_graph, "run", run)
    response = DeferredActionBatchResponse(batch_id=batch.id)
    if response_mode == "live":
        with pytest.raises(RuntimeError, match="no longer awaiting a response"):
            await app.kick(response)
        run.assert_not_awaited()
    else:
        await app.kick(response)
        run.assert_awaited_once()
