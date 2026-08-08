from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config.channels import AgentModelConfig, TrunklineChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.agents.inkling import InklingTentacle
from octomate.tentacles.agents.inkling.base import InklingOutput
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.channels.base import ChannelTentacle
from octomate.tentacles.channels.web.trunkline import (
    TrunklineTentacle,
    build_trunkline_router,
)
from octomate.tentacles.channels.web.trunkline.base import (
    ROUTE_SEP,
    RouteLockedError,
    TrunklineDirective,
)
from tests.support.agents import build_non_stream_agent, build_scripted_agent

# The console drives one configured reception agent through octomate.kick.
RECEPTION_MODEL = "deepseek:deepseek-v4-pro"


async def _register(
    octomate: Octomate, agent: Agent[None, InklingOutput]
) -> TrunklineTentacle:
    assert agent.model is not None
    octomate.connect(
        InklingTentacle(
            "inkling",
            octomate,
            agent=agent,
            models={RECEPTION_MODEL: agent.model},
        )
    )
    channel = octomate.connect(
        TrunklineTentacle(
            "trunkline",
            octomate,
            config=TrunklineChannelConfig(
                agents=[AgentModelConfig(agent="inkling", model=RECEPTION_MODEL)],
            ),
        )
    )
    assert isinstance(channel, TrunklineTentacle)
    await channel.probe()
    return channel


async def _drain(response: StreamingResponse) -> str:
    parts: list[bytes] = []
    async for part in response.body_iterator:
        parts.append(part.encode() if isinstance(part, str) else bytes(part))
    return b"".join(parts).decode()


async def _post(
    channel: TrunklineTentacle,
    prompt: str,
    *,
    thread_id: str = "thread-1",
    model: str | None = None,
) -> str:
    response = await channel.handle_directive(
        TrunklineDirective(thread_id=thread_id, text=prompt, model=model)
    )
    return await _drain(response)


def _events(payload: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in payload.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line.removeprefix("data: ")))
    return events


def _streamed_text(payload: str) -> str:
    """Concatenate the reply text from the native part events."""
    deltas: list[str] = []
    for event in _events(payload):
        match event:
            case {"event_kind": "part_start", "part": {"part_kind": "text"}}:
                part = event["part"]
                assert isinstance(part, dict)
                deltas.append(str(part["content"]))
            case {"event_kind": "part_delta", "delta": {"part_delta_kind": "text"}}:
                delta = event["delta"]
                assert isinstance(delta, dict)
                deltas.append(str(delta["content_delta"]))
    return "".join(deltas)


def _console_address(thread_id: str = "thread-1") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="trunkline",
        chat_type="private",
        chat_id="dev",
        user_id="dev",
        thread_id=thread_id,
    )


def test_trunkline_router_requires_registered_channel() -> None:
    octomate = Octomate()

    with pytest.raises(ValueError, match="TrunklineTentacle"):
        build_trunkline_router(octomate, channel_id="trunkline")

    channel = TrunklineTentacle("trunkline", octomate, config=TrunklineChannelConfig())
    assert isinstance(channel, ChannelTentacle)
    # connect mounts the channel's router (TrunklineTentacle.routers) — no
    # manual include.
    octomate.connect(channel)

    app = octomate.app()
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/trunkline/routes" in paths
    assert "/api/trunkline/threads" in paths
    assert "/api/trunkline/threads/{thread_id}" in paths
    assert "/api/trunkline/threads/{thread_key}/messages" in paths
    assert "/api/trunkline/batches/{batch_id}/resolve" in paths


def test_static_mounts_respect_serve_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    octomate = Octomate()
    monkeypatch.setattr(TrunklineTentacle, "CONSOLE_DIST", tmp_path)

    served = TrunklineTentacle("trunkline", octomate, config=TrunklineChannelConfig())
    assert [mount.path for mount in served.static_mounts()] == ["/"]

    skipped = TrunklineTentacle(
        "trunkline2", octomate, config=TrunklineChannelConfig(serve_console=False)
    )
    assert skipped.static_mounts() == ()


async def test_directive_streams_native_events(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = await _register(octomate, agent)

    payload = await _post(channel, "hi there")
    kinds = [event["event_kind"] for event in _events(payload)]

    assert "part_start" in kinds
    assert kinds[-1] == "run_result"
    assert "run_error" not in kinds
    assert "all done!" in _streamed_text(payload)
    run_result = _events(payload)[-1]
    usage = run_result["usage"]
    assert isinstance(usage, dict)
    assert usage["requests"] >= 1


async def test_directive_streams_every_text_delta(
    in_memory_engine: AsyncEngine,
) -> None:
    """A token-streaming reply must reach the console in full — not just the
    first token before the model's FinalResultEvent."""
    tokens = ["Another", " day", " dawns", " bright", "."]

    async def stream_text(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        for token in tokens:
            yield token

    agent: Agent[None, InklingOutput] = Agent(
        FunctionModel(stream_function=stream_text, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        system_prompt=SYSTEM_PROMPT,
    )
    channel = await _register(Octomate(), agent)

    payload = await _post(channel, "write a poem")

    assert _streamed_text(payload) == "".join(tokens)


async def test_directive_records_chat_ledger(
    in_memory_engine: AsyncEngine,
) -> None:
    """The console follows the channel pattern: the inbound directive and the
    agent reply land in the shared thread ledger, not a separate history path."""
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = await _register(octomate, agent)

    await _post(channel, "hi there")

    thread = await octomate.thread_manager.ensure(_console_address())
    messages = list(thread.messages)
    inbound = [message for message in messages if message.direction == "inbound"]
    outbound = [message for message in messages if message.direction == "outbound"]

    assert [message.message_text for message in inbound] == ["hi there"]
    assert [message.message_text for message in outbound] == ["all done!"]
    assert thread.active_agent_tentacle_id == "inkling"


async def _register_routes(
    octomate: Octomate, agents: dict[str, Agent[None, InklingOutput]]
) -> TrunklineTentacle:
    receptions = []
    for agent_id, agent in agents.items():
        assert agent.model is not None
        octomate.connect(
            InklingTentacle(
                agent_id, octomate, agent=agent, models={RECEPTION_MODEL: agent.model}
            )
        )
        receptions.append(AgentModelConfig(agent=agent_id, model=RECEPTION_MODEL))
    channel = octomate.connect(
        TrunklineTentacle(
            "trunkline", octomate, config=TrunklineChannelConfig(agents=receptions)
        )
    )
    assert isinstance(channel, TrunklineTentacle)
    await channel.probe()
    return channel


async def test_selected_route_routes_to_and_owns_the_chosen_agent(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    inkling, _ = build_scripted_agent(["from inkling"])
    claude, _ = build_scripted_agent(["from claude"])
    channel = await _register_routes(octomate, {"inkling": inkling, "claude": claude})

    payload = await _post(channel, "hello", model=f"claude{ROUTE_SEP}{RECEPTION_MODEL}")

    assert "from claude" in _streamed_text(payload)
    thread = await octomate.thread_manager.ensure(_console_address())
    assert thread.active_agent_tentacle_id == "claude"


async def test_unchanged_selection_does_not_re_handoff(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    channel = await _register_routes(
        octomate,
        {"inkling": build_non_stream_agent(), "claude": build_non_stream_agent()},
    )
    selected = f"claude{ROUTE_SEP}{RECEPTION_MODEL}"

    await _post(channel, "one", model=selected)
    await _post(channel, "two", model=selected)

    thread = await octomate.thread_manager.ensure(_console_address())
    assert [handoff.to_agent_tentacle_id for handoff in thread.handoffs] == ["claude"]


async def test_route_change_after_first_directive_is_refused(
    in_memory_engine: AsyncEngine,
) -> None:
    """The route is fixed once the thread has an owner — a mid-thread model
    switch busts the provider KV cache, so it needs a manual handoff."""
    octomate = Octomate()
    channel = await _register_routes(
        octomate,
        {"inkling": build_non_stream_agent(), "claude": build_non_stream_agent()},
    )

    await _post(channel, "one", model=f"claude{ROUTE_SEP}{RECEPTION_MODEL}")
    with pytest.raises(RouteLockedError, match="manual handoff"):
        await _post(channel, "two", model=f"inkling{ROUTE_SEP}{RECEPTION_MODEL}")

    thread = await octomate.thread_manager.ensure(_console_address())
    assert [handoff.to_agent_tentacle_id for handoff in thread.handoffs] == ["claude"]


async def test_threads_and_detail_endpoints(
    in_memory_engine: AsyncEngine,
) -> None:
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = await _register(octomate, agent)
    await _post(channel, "triage the failing checks", thread_id="thread-9")
    # The console reads every channel's threads, not only its own.
    await octomate.thread_manager.ensure(
        ChannelAddress(
            channel_tentacle_id="slack",
            chat_type="group",
            chat_id="C123",
            user_id="U1",
            thread_id="171234.5678",
        )
    )

    transport = httpx.ASGITransport(app=octomate.app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        listing = await client.get("/api/trunkline/threads")
        assert listing.status_code == 200
        summaries = listing.json()
        by_channel = {summary["channel"]: summary for summary in summaries}
        assert set(by_channel) == {"trunkline", "slack"}
        console = by_channel["trunkline"]
        assert console["thread_key"] == "thread-9"
        assert console["title"] == "triage the failing checks"
        assert console["agent"] == "inkling"
        assert console["message_count"] == 2
        assert console["updated_at"].endswith("Z") or "+" in console["updated_at"]
        assert by_channel["slack"]["thread_key"] == "171234.5678"

        detail = await client.get(f"/api/trunkline/threads/{console['id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert [entry["text"] for entry in body["entries"]] == [
            "triage the failing checks",
            "all done!",
        ]
        assert [entry["direction"] for entry in body["entries"]] == [
            "inbound",
            "outbound",
        ]
        assert [session["to_agent"] for session in body["sessions"]] == ["inkling"]
        assert body["pending"] == []
        # The recorded run replays as the wire events its live stream carried,
        # so a reload folds through the same processor as live streaming.
        [run] = body["runs"]
        assert run["agent"] == "inkling"
        kinds = [event["event_kind"] for event in run["events"]]
        assert kinds[-1] == "run_result"
        replayed = [
            event["part"]["content"]
            for event in run["events"]
            if event["event_kind"] == "part_start"
            and event["part"]["part_kind"] == "text"
        ]
        assert replayed == ["all done!"]

        # Any channel's thread is readable through the same endpoint.
        foreign = await client.get(
            f"/api/trunkline/threads/{by_channel['slack']['id']}"
        )
        assert foreign.status_code == 200
        assert foreign.json()["channel"] == "slack"
        assert foreign.json()["runs"] == []

        missing = await client.get(f"/api/trunkline/threads/{uuid.UUID(int=7)}")
        assert missing.status_code == 404

        connected = await client.get("/api/trunkline/channels")
        assert connected.status_code == 200
        assert [c["id"] for c in connected.json()] == ["trunkline"]

        routes = await client.get("/api/trunkline/routes")
        assert routes.json() == [
            {
                "id": f"inkling{ROUTE_SEP}{RECEPTION_MODEL}",
                "agent": "inkling",
                "model": RECEPTION_MODEL,
            }
        ]


async def test_batch_resolve_resolves_and_streams(
    in_memory_engine: AsyncEngine,
) -> None:
    """Resolving a deferred batch resolves its actions and answers with an
    SSE stream (the resumed run's events, or a run_error if resume fails)."""
    octomate = Octomate()
    agent, _ = build_scripted_agent(["resumed"])
    channel = await _register(octomate, agent)
    await _post(channel, "kick off", thread_id="thread-2")

    thread = await octomate.thread_manager.ensure(_console_address("thread-2"))
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    requests = DeferredToolRequests(
        approvals=[
            ToolCallPart(
                tool_name="dangerous_tool",
                args={"path": "/tmp/x"},
                tool_call_id="tc-1",
            )
        ]
    )
    batch = await octomate.deferred_actions.create_batch(
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=_console_address("thread-2"),
        target_address=_console_address("thread-2"),
        target_mode="main",
        decision=None,
        requests=requests,
    )
    approval_id = next(iter(batch.approvals)).id

    transport = httpx.ASGITransport(app=octomate.app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        missing = await client.post(
            "/api/trunkline/batches/00000000-0000-0000-0000-000000000000/resolve",
            json={"approvals": {}},
        )
        assert missing.status_code == 404

        response = await client.post(
            f"/api/trunkline/batches/{batch.id}/resolve",
            json={"approvals": {str(approval_id): True}},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        # A resolved batch must not resume twice.
        again = await client.post(
            f"/api/trunkline/batches/{batch.id}/resolve",
            json={"approvals": {str(approval_id): True}},
        )
        assert again.status_code == 409

    resolved = await octomate.deferred_actions.get_batch(batch.id)
    assert next(iter(resolved.approvals)).status == "approved"
    assert resolved.status in {"resolved", "resuming", "completed"}
