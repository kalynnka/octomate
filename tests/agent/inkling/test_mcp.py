"""Inkling uses the served MCP contract with user credentials and core-owned scope."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastmcp.exceptions import ToolError
from pydantic import JsonValue
from pydantic_ai.capabilities import Toolset
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.base import OctomateConfig
from octomate.events import TodoCompletedEvent, TodoCreatedEvent
from octomate.fibers import MemoryFiber
from octomate.managers.gateway import OctomateSession
from octomate.managers.todos import TodoManager
from octomate.mcp.gateway import CONVERSATION_HEADER
from octomate.nerve import ThreadTopic
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.messages import ModelRequest
from octomate.schemas.segments import MessageSegment
from octomate.schemas.thread import ThreadKey
from octomate.schemas.triage import AgentRoute, Claim
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.agent import Agent
from octomate.tentacles.inkling.base import InklingOutput
from tests.agent.test_mcp_serving import over, served
from tests.support.agents import FakeAgent
from tests.support.managers import FakeConversationManager
from tests.support.mcp import served_inkling_mcp
from tests.support.users import a_api_key, a_user, auth_config


async def a_turn(*, authenticated: bool = True) -> tuple[Octomate, OctomateSession]:
    await a_user("alice", profiles={"im": "alice"})
    host = Octomate(
        config=OctomateConfig(auth=auth_config() if authenticated else None)
    )
    address = ChannelAddress(
        channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
    )
    thread = await host.threads.ensure(address)
    conversation = await host.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    scope = OctomateSession(
        channel_routes={},
        current_agent_id="inkling",
        conversation_id=conversation.id,
        thread_id=thread.id,
        conversation_address=address,
        user_profile=await host.users.profile("im", "alice"),
        nerve=host.nerve,
    )
    return host, scope


async def test_inkling_calls_todos_through_authenticated_mcp_and_publishes_once(
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, scope = await a_turn()
    steps = iter(
        (
            ("load_capability", {"id": "todos"}),
            (
                "add_todo",
                {
                    "content": "Check the boundary",
                    "active_form": "Checking the boundary",
                },
            ),
        )
    )
    offered: list[list[str]] = []

    async def respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        offered.append([tool.name for tool in info.function_tools])
        call = next(steps, None)
        if call is not None:
            name, arguments = call
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}
        else:
            yield "Done"

    model = FunctionModel(stream_function=respond, model_name="scripted")
    agent: Agent[None, InklingOutput] = Agent(
        model, output_type=[str, list[MessageSegment], DeferredToolRequests]
    )
    inkling = InklingTentacle(
        "inkling", host, auth=host.auth, habitats=host.habitats, agent=agent
    )
    assert scope.conversation_address is not None
    topic = ThreadTopic(ThreadKey.from_address(scope.conversation_address))
    async with (
        served_inkling_mcp(host, monkeypatch),
        inkling,
        host.gateway.driving(scope),
        host.nerve.subscribe(topic) as events,
    ):
        result = await inkling.run(
            "Make a plan",
            thread_id=scope.thread_id,
            conversation_address=scope.conversation_address,
            conversation_id=scope.conversation_id,
        )
        assert result.output == "Done"
        assert "add_todo" not in offered[0]
        assert "add_todo" in offered[1]
        assert "inspect" in offered[0]
        async with asyncio.timeout(1):
            created = (await events.receive()).event
        assert isinstance(created, TodoCreatedEvent)
        assert isinstance(events, MemoryFiber)
        with pytest.raises(anyio.WouldBlock):
            events.receive_nowait()
        assert scope.conversation_id is not None
        assert [
            todo.ref for todo in await TodoManager().list_todos(scope.conversation_id)
        ] == [created.todo.ref]
        assert host.auth is not None
        assert scope.user_profile is not None
        assert scope.user_profile.user_id is not None
        assert len(await host.auth.list_api_keys(scope.user_profile.user_id)) == 1


async def test_todo_mcp_rejects_another_users_conversation_and_has_no_owner_arguments(
    in_memory_engine: AsyncEngine,
) -> None:
    host, scope = await a_turn()
    bob = await a_user("bob")
    await a_api_key(bob, "bob-token", scopes=["mcp"])
    assert scope.user_profile is not None
    alice = await host.users.owner(scope.user_profile)
    assert alice is not None
    await a_api_key(alice, "alice-token", scopes=["mcp"])
    assert scope.conversation_id is not None
    assert scope.conversation_address is not None
    topic = ThreadTopic(ThreadKey.from_address(scope.conversation_address))

    async with (
        served(host),
        host.gateway.driving(scope),
        host.nerve.subscribe(topic) as events,
    ):
        async with over(
            host,
            host,
            {
                "Authorization": "Bearer bob-token",
                CONVERSATION_HEADER: str(scope.conversation_id),
            },
        ) as client:
            with pytest.raises(ToolError, match="not this bearer's"):
                await client.call_tool(
                    "add_todo", {"content": "Wrong owner", "active_form": "Writing"}
                )
        assert await TodoManager().list_todos(scope.conversation_id) == []

        async with over(
            host,
            host,
            {
                "Authorization": "Bearer alice-token",
                CONVERSATION_HEADER: str(scope.conversation_id),
            },
        ) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools["add_todo"].input_schema["properties"]) == {
                "content",
                "active_form",
            }
            await client.call_tool(
                "add_todo", {"content": "My task", "active_form": "Working"}
            )
            created = (await events.receive()).event
            assert isinstance(created, TodoCreatedEvent)
            await client.call_tool(
                "update_todo_status",
                {"todo_id": created.todo.ref, "status": "completed"},
            )
            changed = [(await events.receive()).event for _ in range(3)]
            assert isinstance(changed[-1], TodoCompletedEvent)


async def test_anonymous_inkling_run_does_not_mount_core_mcp_tools() -> None:
    seen: list[str] = []

    async def respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        seen.extend(tool.name for tool in info.function_tools)
        yield "Bare agent"

    host = Octomate(conversations=FakeConversationManager())
    model = FunctionModel(stream_function=respond, model_name="scripted")
    agent: Agent[None, InklingOutput] = Agent(
        model, output_type=[str, list[MessageSegment], DeferredToolRequests]
    )
    inkling = InklingTentacle(
        "inkling", host, auth=host.auth, habitats=host.habitats, agent=agent
    )
    scope = OctomateSession(channel_routes={}, current_agent_id="inkling")
    result = await inkling.run(
        "Hello",
        thread_id=uuid7(),
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="dm",
            chat_id="anonymous",
            user_id="anonymous",
        ),
        conversation_id=scope.conversation_id,
    )
    assert result.output == "Bare agent"
    assert seen == []
    assert inkling.api_keys == {}


async def test_registered_inkling_run_requires_configured_authentication(
    in_memory_engine: AsyncEngine,
) -> None:
    host, scope = await a_turn(authenticated=False)
    inkling = InklingTentacle(
        "inkling",
        host,
        auth=host.auth,
        habitats=host.habitats,
        models={"test": TestModel()},
    )
    assert scope.conversation_address is not None

    async with host.gateway.driving(scope):
        with pytest.raises(ValueError, match="require configured authentication"):
            await inkling.run(
                "Use my tools",
                thread_id=scope.thread_id,
                conversation_address=scope.conversation_address,
                conversation_id=scope.conversation_id,
            )
    assert inkling.api_keys == {}


async def test_mcp_accomplices_leave_parent_run_attribution_to_core_persistence(
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, initial = await a_turn()
    child = FakeAgent(id="claude", octomate=host, models={"opus": "fake-model"})
    child.reception_output = "Three findings"
    scope = replace(
        initial,
        agents={"claude": child},
        channel_routes={
            "im": [
                AgentRoute(
                    agent_id="claude",
                    model="opus",
                    claim=Claim(ability="audit", efforts=()),
                )
            ]
        },
    )
    assert scope.user_profile is not None
    owner = await host.users.owner(scope.user_profile)
    assert owner is not None
    await a_api_key(owner, "agent-token", scopes=["mcp"])
    linked = AsyncMock()
    monkeypatch.setattr(host.conversations, "link_parent_run", linked)

    async with (
        served(host),
        host.gateway.driving(scope),
        over(
            host,
            host,
            {
                "Authorization": "Bearer agent-token",
                CONVERSATION_HEADER: str(scope.conversation_id),
            },
        ) as client,
    ):
        result = await client.call_tool(
            "gateway_commission",
            {
                "name": "audit",
                "agent_id": "claude",
                "model": "opus",
                "brief": "Audit the boundary",
            },
            meta={"tool_call_id": "call-commission", "run_id": "untrusted-parent"},
        )
        assert result.data == "Three findings"
        assert linked.call_args.kwargs["parent_run_id"] is None
        assert linked.call_args.kwargs["parent_tool_call_id"] == "call-commission"
        assert child.turns[0].thread_id == scope.thread_id
        await client.call_tool(
            "gateway_whisper",
            {"name": "audit", "message": "Check again"},
            meta={"tool_call_id": "call-whisper"},
        )
        assert child.turns[-1].prompt == "Check again"
        assert linked.call_args.kwargs["parent_tool_call_id"] == "call-whisper"
        assert linked.call_args.kwargs["parent_run_id"] is None
        await client.call_tool(
            "gateway_whisper",
            {"name": "audit", "message": "No call identity"},
        )
        assert child.turns[-1].prompt == "No call identity"
        assert linked.call_args.kwargs["parent_tool_call_id"] is None


@pytest.mark.parametrize("outcome", ["complete", "resume", "error"])
async def test_mcp_accomplices_link_to_the_run_that_returns_their_tool_result(
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    host, initial = await a_turn()

    async def report(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "Child report"

    child = InklingTentacle(
        "helper",
        host,
        auth=host.auth,
        habitats=host.habitats,
        models={"test": FunctionModel(stream_function=report)},
        gateway=False,
    )
    scope = replace(
        initial,
        agents={"helper": child},
        channel_routes={
            "im": [
                AgentRoute(
                    agent_id="helper",
                    model="test",
                    claim=Claim(ability="audit", efforts=()),
                )
            ]
        },
    )
    calls: list[tuple[str, dict[str, JsonValue], str]] = [
        (
            "commission",
            {"name": "audit", "agent_id": "helper", "model": "test", "brief": "Audit"},
            "call-commission",
        )
    ]
    if outcome == "resume":
        calls.extend(
            [
                ("approve_step", {}, "call-approval"),
                ("whisper", {"name": "audit", "message": "Follow up"}, "call-whisper"),
            ]
        )
    steps = iter(calls)

    async def respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        call = next(steps, None)
        if call is None:
            if outcome == "error":
                raise RuntimeError("model failed after commissioning")
            yield "Done"
            return
        name, arguments, tool_call_id = call
        yield {
            0: DeltaToolCall(
                name=name, json_args=json.dumps(arguments), tool_call_id=tool_call_id
            )
        }

    def approve_step() -> str:
        """Continue after approval."""
        return "Approved"

    model = FunctionModel(stream_function=respond)
    agent: Agent[None, InklingOutput] = Agent(
        model,
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        capabilities=[Toolset(FunctionToolset([approve_step], requires_approval=True))],
    )
    inkling = InklingTentacle(
        "inkling",
        host,
        auth=host.auth,
        habitats=host.habitats,
        agent=agent,
        permission_mode="bypassPermissions",
    )
    assert scope.conversation_address is not None
    assert scope.conversation_id is not None
    async with served_inkling_mcp(host, monkeypatch), host.gateway.driving(scope):
        with (
            pytest.raises(RuntimeError, match="model failed")
            if outcome == "error"
            else nullcontext()
        ):
            await inkling.run(
                "Delegate the audit",
                thread_id=scope.thread_id,
                conversation_address=scope.conversation_address,
                conversation_id=scope.conversation_id,
            )

    parent = await host.conversations.get(scope.conversation_id)
    owners = {
        part.tool_call_id: run.id
        for run in parent.runs
        for message in run.messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    [accomplice] = await host.conversations.subagents(scope.conversation_id)
    stored = await host.conversations.get(accomplice.id)
    expected = {"call-commission"}
    if outcome == "resume":
        expected.add("call-whisper")
        assert owners["call-commission"] != owners["call-whisper"]
    assert {run.parent_tool_call_id for run in stored.runs} == expected
    for run in stored.runs:
        assert run.parent_tool_call_id is not None
        assert run.parent_run_id == owners[run.parent_tool_call_id]
