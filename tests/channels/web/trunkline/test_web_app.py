from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic_ai.messages import ModelMessage, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config.channels import AgentModelConfig, TrunklineChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelRequest
from octomate.schemas.project import Project
from octomate.schemas.segments import MessageSegment, TextSegment
from octomate.schemas.thread import CLAUDE_NATIVE_ID, ThreadKey
from octomate.schemas.user import UserProfile
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
from tests.support.managers import a_registry

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
        chat_type="thread" if thread_id else "dm",
        chat_id="dev",
        user_id="dev",
        channel_thread_id=thread_id,
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
    assert "/api/trunkline/threads/{thread_id}/messages" in paths
    assert "/api/trunkline/threads/{thread_id}/conversations" in paths
    assert "/api/trunkline/threads/{thread_id}/project" in paths
    assert "/api/trunkline/threads/{thread_id}/batches" in paths
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
            chat_type="thread",
            chat_id="C123",
            user_id="U1",
            channel_thread_id="171234.5678",
        )
    )

    transport = httpx.ASGITransport(app=octomate.app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        listing = await client.get("/api/trunkline/threads")
        assert listing.status_code == 200
        threads = listing.json()
        by_channel = {thread["channel_tentacle_id"]: thread for thread in threads}
        assert set(by_channel) == {"trunkline", "slack"}
        console = by_channel["trunkline"]
        assert console["channel_thread_id"] == "thread-9"
        # The ledger is its own request: a listing that carried every thread's
        # messages would be the ledger.
        assert "messages" not in console
        assert [h["to_agent_tentacle_id"] for h in console["handoffs"]] == ["inkling"]
        assert console["updated_at"].endswith("Z") or "+" in console["updated_at"]
        assert by_channel["slack"]["channel_thread_id"] == "171234.5678"

        detail = await client.get(f"/api/trunkline/threads/{console['id']}")
        assert detail.status_code == 200
        assert "messages" not in detail.json()

        messages = await client.get(f"/api/trunkline/threads/{console['id']}/messages")
        assert messages.status_code == 200
        ledger = messages.json()
        assert [entry["message_text"] for entry in ledger] == [
            "triage the failing checks",
            "all done!",
        ]
        assert [entry["direction"] for entry in ledger] == ["inbound", "outbound"]
        assert [entry["sender"]["name"] for entry in ledger] == ["Console", "Octomate"]
        # The model ledger is the agent's own history, never a reader's.
        assert all("model_messages" not in entry for entry in ledger)

        conversations = await client.get(
            f"/api/trunkline/threads/{console['id']}/conversations"
        )
        assert conversations.status_code == 200
        [conversation] = conversations.json()
        assert conversation["agent_tentacle_id"] == "inkling"
        assert "messages" not in conversation
        [run] = conversation["runs"]
        assert run["kind"] == "octomate"
        assert run["started_at"].endswith("Z") or "+" in run["started_at"]
        assert "messages" not in run

        batches = await client.get(f"/api/trunkline/threads/{console['id']}/batches")
        assert batches.json() == []

        # Any channel's thread is readable through the same endpoints.
        foreign_id = by_channel["slack"]["id"]
        foreign = await client.get(f"/api/trunkline/threads/{foreign_id}")
        assert foreign.status_code == 200
        assert foreign.json()["channel_tentacle_id"] == "slack"
        assert (
            await client.get(f"/api/trunkline/threads/{foreign_id}/conversations")
        ).json() == []

        # Every read hangs off a thread, so a stray id is a 404 on all of them
        # rather than an empty list that reads as "nothing here yet".
        stray = uuid.UUID(int=7)
        for suffix in ("", "/messages", "/conversations", "/project", "/batches"):
            missing = await client.get(f"/api/trunkline/threads/{stray}{suffix}")
            assert missing.status_code == 404, suffix

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


async def test_console_reads_never_load_the_model_ledger(
    in_memory_engine: AsyncEngine,
) -> None:
    """Keeping the transcript out of the payload is not keeping it out of the
    query. Every message relation on this path is lazy="selectin", so without
    the loader options each read would fetch every model message the thread ever
    produced and then drop it on the floor."""
    octomate = Octomate()
    agent, _ = build_scripted_agent(["all done!"])
    channel = await _register(octomate, agent)
    await _post(channel, "triage the failing checks", thread_id="thread-9")
    thread = await octomate.thread_manager.ensure(_console_address("thread-9"))

    selects: list[str] = []

    @sqlalchemy_event.listens_for(in_memory_engine.sync_engine, "before_cursor_execute")
    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    transport = httpx.ASGITransport(app=octomate.app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        base = f"/api/trunkline/threads/{thread.id}"
        for url in (
            "/api/trunkline/threads",
            base,
            f"{base}/messages",
            f"{base}/conversations",
            f"{base}/project",
            f"{base}/batches",
        ):
            selects.clear()
            assert (await client.get(url)).status_code == 200
            assert not any("model_messages" in select for select in selects), url

        # The listing names threads and opens none of them.
        selects.clear()
        await client.get("/api/trunkline/threads")
        assert not any("thread_messages" in select for select in selects)
        assert not any("FROM conversations" in select for select in selects)


async def test_a_native_thread_reads_back_with_its_project_and_run_directory(
    in_memory_engine: AsyncEngine,
) -> None:
    """A session Octomate did not drive is a thread like any other here: the
    console lists it, reads its ledger, and sees where the work happened — the
    project the thread is filed under and the directory each run ran in."""
    octomate = Octomate()
    octomate.connect(
        TrunklineTentacle("trunkline", octomate, config=TrunklineChannelConfig())
    )
    project = Project(root=Path("/srv/inky"), origin="codex")
    octomate.projects = await a_registry(project)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "thread", "session-1"), project=project
    )
    await octomate.thread_manager.record_inbound(
        MessageEvent(
            tentacle_id=CLAUDE_NATIVE_ID,
            message_id="turn-1",
            chat_id="session-1",
            chat_type="thread",
            user_id="native",
            sender=UserProfile(channel_user_id="native", name="native"),
            segments=[TextSegment(data={"text": "read the migration"})],
        )
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
    )
    await octomate.conversations.record_external_run(
        conversation,
        run_id="turn-1",
        messages=[
            ModelRequest(
                parts=[UserPromptPart(content="read the migration")],
                timestamp=datetime(2026, 8, 9, 12, tzinfo=UTC),
            )
        ],
        name=CLAUDE_NATIVE_ID,
        # A run drifts into a subdirectory of the project it belongs to.
        cwd=Path("/srv/inky/migrations"),
        external_session_id="session-1",
    )

    transport = httpx.ASGITransport(app=octomate.app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        [listed] = (await client.get("/api/trunkline/threads")).json()
        assert listed["channel_tentacle_id"] == CLAUDE_NATIVE_ID
        assert listed["kind"] == "native_thread"
        # The runtime's own session id: what its editor extension reopens by.
        assert listed["chat_id"] == "session-1"

        thread_url = f"/api/trunkline/threads/{listed['id']}"
        registered = (await client.get(f"{thread_url}/project")).json()
        assert (registered["name"], registered["root"]) == ("inky", "/srv/inky")
        [conversation] = (await client.get(f"{thread_url}/conversations")).json()
        [run] = conversation["runs"]
        assert run["kind"] == "external"
        assert run["cwd"] == "/srv/inky/migrations"
        assert run["external_session_id"] == "session-1"


async def test_a_thread_no_project_claims_reads_back_without_one(
    in_memory_engine: AsyncEngine,
) -> None:
    """A directive on the console's own channel is not work in a project — and
    a run that reported no directory says so rather than guessing one."""
    octomate = Octomate()
    agent, _ = build_scripted_agent(["done"])
    channel = await _register(octomate, agent)
    await _post(channel, "what changed?", thread_id="thread-8")

    transport = httpx.ASGITransport(app=octomate.app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        [listed] = (await client.get("/api/trunkline/threads")).json()
        assert listed["project_id"] is None
        thread_url = f"/api/trunkline/threads/{listed['id']}"
        assert (await client.get(f"{thread_url}/project")).json() is None
        [conversation] = (await client.get(f"{thread_url}/conversations")).json()
        assert [run["cwd"] for run in conversation["runs"]] == [None]


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
        # A reload finds the waiting feeler on the thread it blocks.
        waiting = await client.get(f"/api/trunkline/threads/{thread.id}/batches")
        [pending] = waiting.json()
        assert pending["id"] == str(batch.id)
        assert pending["status"] == "pending"
        # The tool-call payload is the agent's; the action carries what a reader
        # needs to render the card.
        assert "requests" not in pending
        [approval] = pending["approvals"]
        assert approval["id"] == str(approval_id)
        assert approval["args"]["tool_name"] == "dangerous_tool"

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
