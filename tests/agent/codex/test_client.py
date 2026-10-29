"""The shared SDK transport must keep reading while a human is deciding."""

import asyncio
from base64 import b64encode
from collections.abc import AsyncGenerator
from queue import Queue
from typing import NamedTuple
from unittest.mock import AsyncMock

import pytest
from httpx import URL
from openai_codex import CodexConfig
from openai_codex._message_router import _TurnSubscription
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import (
    McpToolCallThreadItem,
    ReviewTarget,
    UncommittedChangesReviewTarget,
)
from openai_codex.models import InitializeResponse

from octomate.config.agents import CodexConfig as AgentCodexConfig
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.codex.ink import CodexInk
from octomate.types.json import JsonObject


class CommandTransport(NamedTuple):
    ink: CodexInk
    incoming: Queue[JsonObject | None]
    outgoing: Queue[JsonObject]


@pytest.fixture
async def command_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[CommandTransport]:
    incoming: Queue[JsonObject | None] = Queue()
    outgoing: Queue[JsonObject] = Queue()

    def read(client: CodexClient) -> JsonObject:
        message = incoming.get(timeout=5)
        if message is None:
            raise TransportClosedError("closed")
        return message

    monkeypatch.setattr(CodexClient, "_read_message", read)
    monkeypatch.setattr(
        CodexClient, "_write_message", lambda client, message: outgoing.put(message)
    )
    monkeypatch.setattr(AsyncCodexClient, "start", AsyncMock())
    monkeypatch.setattr(
        AsyncCodexClient,
        "initialize",
        AsyncMock(return_value=InitializeResponse(userAgent="codex/0.160.0")),
    )
    client = SharedCodex(CodexConfig(), AsyncMock(return_value={}), instrument=False)
    ink = CodexInk(
        AgentCodexConfig(),
        agent_id="codex",
        mcp_url=URL("http://example.com/mcp"),
        handler=AsyncMock(return_value={}),
    )
    ink.client = client
    async with client:
        reader = asyncio.create_task(asyncio.to_thread(client.transport._reader_loop))
        try:
            yield CommandTransport(ink, incoming, outgoing)
        finally:
            incoming.put(None)
            await reader


@pytest.fixture
def mcp_app_call() -> JsonObject:
    return {
        "type": "mcpToolCall",
        "id": "call-a",
        "server": "apps",
        "tool": "show_chart",
        "arguments": {"city": "上海"},
        "status": "completed",
        "result": {"content": [], "structuredContent": {"temperature": 20}},
        "mcpAppUi": {
            "resourceUri": "ui://chart/current.html",
            "preferredModelDisplayMode": "inline",
        },
        "appContext": {
            "connectorId": "weather",
            "linkId": "account-a",
            "resourceUri": "ui://chart/context.html",
        },
        "mcpAppResourceUri": "ui://chart/legacy.html",
    }


@pytest.mark.parametrize("source", ["current", "context", "legacy"])
@pytest.mark.parametrize("encoded", [False, True])
async def test_app_resource_reads_advertised_ui_with_original_scope(
    command_transport: CommandTransport,
    mcp_app_call: JsonObject,
    source: str,
    encoded: bool,
) -> None:
    ink, incoming, outgoing = command_transport
    item = McpToolCallThreadItem.model_validate(mcp_app_call)
    if source != "current":
        item.mcp_app_ui = None
    if source == "legacy":
        assert item.app_context is not None
        item.app_context.resource_uri = None
    uri = f"ui://chart/{source}.html"
    reading = asyncio.create_task(ink.app_resource("thread-a", item))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    assert request["method"] == "mcpServer/resource/read"
    assert request["params"] == {
        "threadId": "thread-a",
        "server": "apps",
        "uri": uri,
        "originCallId": "call-a",
        "target": {"connectorId": "weather", "linkId": "account-a"},
    }
    html = "<html><body>上海</body></html>"
    content: JsonObject = {
        "uri": uri,
        "mimeType": "text/html;profile=mcp-app",
        "_meta": {"ui": {"csp": {"connectDomains": ["https://api.example.com"]}}},
    }
    content.update(
        {"blob": b64encode(html.encode()).decode()} if encoded else {"text": html}
    )
    incoming.put(
        {
            "id": request["id"],
            "result": {"originCallId": "call-a", "contents": [content]},
        }
    )
    resource = await asyncio.wait_for(reading, 1)
    assert resource is not None
    assert str(resource.uri) == uri
    assert resource.html == html
    assert resource.ui.csp is not None
    assert resource.ui.csp.connect_domains == ["https://api.example.com"]
    assert outgoing.empty()


@pytest.mark.parametrize("context", [None, {"connectorId": "weather", "linkId": None}])
async def test_app_resource_distinguishes_local_server_and_no_auth_target(
    command_transport: CommandTransport,
    mcp_app_call: JsonObject,
    context: JsonObject | None,
) -> None:
    ink, incoming, outgoing = command_transport
    item = McpToolCallThreadItem.model_validate({**mcp_app_call, "appContext": context})
    reading = asyncio.create_task(ink.app_resource("thread-a", item))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    expected: JsonObject = {
        "threadId": "thread-a",
        "server": "apps",
        "uri": "ui://chart/current.html",
        "originCallId": "call-a",
    }
    if context is not None:
        expected["target"] = context
    assert request["params"] == expected
    incoming.put(
        {
            "id": request["id"],
            "result": {
                "contents": [
                    {
                        "uri": "ui://chart/current.html",
                        "mimeType": "text/html;profile=mcp-app",
                        "text": "<html>chart</html>",
                    }
                ]
            },
        }
    )
    assert await asyncio.wait_for(reading, 1) is not None


@pytest.mark.parametrize(
    "update",
    [
        {"status": "inProgress"},
        {"status": "failed"},
        {"result": None},
        {"mcpAppUi": None, "appContext": None, "mcpAppResourceUri": None},
    ],
)
async def test_app_resource_skips_calls_without_a_completed_ui(
    command_transport: CommandTransport, mcp_app_call: JsonObject, update: JsonObject
) -> None:
    ink, _, outgoing = command_transport
    item = McpToolCallThreadItem.model_validate({**mcp_app_call, **update})
    assert await ink.app_resource("thread-a", item) is None
    assert outgoing.empty()


async def test_app_resource_rejects_non_ui_uri_before_reading(
    command_transport: CommandTransport, mcp_app_call: JsonObject
) -> None:
    ink, _, outgoing = command_transport
    item = McpToolCallThreadItem.model_validate(mcp_app_call)
    assert item.mcp_app_ui is not None
    item.mcp_app_ui.resource_uri = "file:///private/report.html"
    with pytest.raises(ValueError, match="must use the ui scheme"):
        await ink.app_resource("thread-a", item)
    assert outgoing.empty()


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"uri": "ui://another/chart.html"}, "URI or MIME type does not match"),
        ({"mimeType": "text/html"}, "URI or MIME type does not match"),
        ({"text": " "}, "HTML must not be empty"),
        ({"blob": "not base64!"}, "Only base64 data is allowed"),
        ({"blob": "/w=="}, "invalid start byte"),
        ({"_meta": []}, "Input should be a valid dictionary"),
        (
            {"_meta": {"ui": {"csp": {"connectDomains": ["'unsafe-inline'"]}}}},
            "CSP sources must be HTTP",
        ),
    ],
)
async def test_app_resource_rejects_invalid_content(
    command_transport: CommandTransport,
    mcp_app_call: JsonObject,
    update: JsonObject,
    message: str,
) -> None:
    ink, incoming, outgoing = command_transport
    item = McpToolCallThreadItem.model_validate(mcp_app_call)
    reading = asyncio.create_task(ink.app_resource("thread-a", item))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    content: JsonObject = {
        "uri": "ui://chart/current.html",
        "mimeType": "text/html;profile=mcp-app",
        "text": "<html>chart</html>",
        **update,
    }
    if "blob" in update:
        del content["text"]
    incoming.put({"id": request["id"], "result": {"contents": [content]}})
    with pytest.raises(ValueError, match=message):
        await asyncio.wait_for(reading, 1)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"originCallId": "another-call", "contents": []}, "another call"),
        ({"contents": []}, "exactly one"),
        (
            {"contents": [{"uri": "ui://a", "text": "a"}] * 2},
            "exactly one",
        ),
    ],
)
async def test_app_resource_rejects_mismatched_response(
    command_transport: CommandTransport,
    mcp_app_call: JsonObject,
    result: JsonObject,
    message: str,
) -> None:
    ink, incoming, outgoing = command_transport
    item = McpToolCallThreadItem.model_validate(mcp_app_call)
    reading = asyncio.create_task(ink.app_resource("thread-a", item))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    incoming.put({"id": request["id"], "result": result})
    with pytest.raises(ValueError, match=message):
        await asyncio.wait_for(reading, 1)


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("early", [False, True])
async def test_command_turn_retains_events_before_and_after_ack(
    command_transport: CommandTransport, review: bool, early: bool
) -> None:
    ink, incoming, outgoing = command_transport
    target = (
        ReviewTarget(UncommittedChangesReviewTarget(type="uncommittedChanges"))
        if review
        else None
    )
    starting = asyncio.create_task(ink.start_command("alice", target))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    assert request["method"] == ("review/start" if review else "thread/compact/start")
    turn: JsonObject = {
        "id": "turn-a",
        "status": "completed",
        "items": [],
        "itemsView": "full",
    }
    response: JsonObject = {
        "id": request["id"],
        "result": {"reviewThreadId": "alice", "turn": turn} if review else {},
    }
    if not early:
        incoming.put(response)
    incoming.put(
        {
            "method": "turn/started",
            "params": {"threadId": "alice", "turn": {**turn, "status": "inProgress"}},
        }
    )
    incoming.put(
        {"method": "turn/completed", "params": {"threadId": "alice", "turn": turn}}
    )
    if early:
        incoming.put(response)
    handle = await asyncio.wait_for(starting, 1)
    assert handle.thread_id == "alice"
    assert handle.id == "turn-a"
    events = [event async for event in handle.stream()]
    assert [event.method for event in events] == ["turn/started", "turn/completed"]
    assert not ink.client.transport.compact_starts


async def test_compaction_does_not_return_at_ack_or_take_another_threads_turn(
    command_transport: CommandTransport,
) -> None:
    ink, incoming, outgoing = command_transport
    starting = asyncio.create_task(ink.start_command("alice"))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    incoming.put({"id": request["id"], "result": {}})
    turn: JsonObject = {
        "id": "turn-b",
        "status": "inProgress",
        "items": [],
        "itemsView": "full",
    }
    incoming.put(
        {"method": "turn/started", "params": {"threadId": "bob", "turn": turn}}
    )
    incoming.put({"method": "skills/changed", "params": {}})
    await asyncio.wait_for(ink.client._client.next_notification(), 1)
    assert not starting.done()
    incoming.put(None)
    with pytest.raises(TransportClosedError, match="before compaction started"):
        await asyncio.wait_for(starting, 1)
    assert not ink.client.transport.compact_starts


async def test_review_rejects_another_thread_in_the_response(
    command_transport: CommandTransport,
) -> None:
    ink, incoming, outgoing = command_transport
    target = ReviewTarget(UncommittedChangesReviewTarget(type="uncommittedChanges"))
    starting = asyncio.create_task(ink.start_command("alice", target))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    incoming.put(
        {
            "id": request["id"],
            "result": {
                "reviewThreadId": "bob",
                "turn": {
                    "id": "turn-b",
                    "status": "inProgress",
                    "items": [],
                    "itemsView": "full",
                },
            },
        }
    )
    with pytest.raises(ValueError, match="another thread"):
        await asyncio.wait_for(starting, 1)


async def test_canceled_command_start_releases_late_subscription(
    command_transport: CommandTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ink, incoming, outgoing = command_transport
    closed = asyncio.Event()
    close = _TurnSubscription.close

    def release(subscription: _TurnSubscription) -> None:
        close(subscription)
        closed.set()

    monkeypatch.setattr(_TurnSubscription, "close", release)
    starting = asyncio.create_task(ink.start_command("alice"))
    request = await asyncio.to_thread(outgoing.get, True, 1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    turn: JsonObject = {
        "id": "turn-a",
        "status": "inProgress",
        "items": [],
        "itemsView": "full",
    }
    incoming.put(
        {"method": "turn/started", "params": {"threadId": "alice", "turn": turn}}
    )
    incoming.put({"id": request["id"], "result": {}})
    await asyncio.wait_for(closed.wait(), 1)
    assert not ink.client.transport._router._pending_turn_requests
    assert not ink.client.transport._router._turn_states


def test_constructing_shared_client_needs_no_event_loop_or_process() -> None:
    async def approve(method: str, params: JsonObject | None) -> JsonObject:
        return {}

    client = SharedCodex(CodexConfig(), approve, instrument=False)
    assert client.transport._proc is None
    assert not client._initialized


async def test_reader_delivers_other_responses_and_events_during_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming: Queue[JsonObject | None] = Queue()
    outgoing: Queue[JsonObject] = Queue()
    requested, release = asyncio.Event(), asyncio.Event()

    async def approve(method: str, params: JsonObject | None) -> JsonObject:
        assert params == {"threadId": "alice"}
        requested.set()
        await release.wait()
        return {"decision": "accept"}

    def read(client: CodexClient) -> JsonObject:
        message = incoming.get(timeout=2)
        if message is None:
            raise TransportClosedError("closed")
        return message

    def write(client: CodexClient, message: JsonObject) -> None:
        outgoing.put(message)

    monkeypatch.setattr(CodexClient, "_read_message", read)
    monkeypatch.setattr(CodexClient, "_write_message", write)
    monkeypatch.setattr(AsyncCodexClient, "start", AsyncMock())
    monkeypatch.setattr(
        AsyncCodexClient,
        "initialize",
        AsyncMock(return_value=InitializeResponse(userAgent="codex/0.158.0")),
    )
    client = SharedCodex(CodexConfig(), approve, instrument=False)
    await client.__aenter__()
    reader = asyncio.create_task(asyncio.to_thread(client.transport._reader_loop))
    try:
        incoming.put(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "alice"},
            }
        )
        await asyncio.wait_for(requested.wait(), 1)
        waiter = client.transport._router.create_response_waiter("models")
        incoming.put({"id": "models", "result": {"data": []}})
        incoming.put({"method": "skills/changed", "params": {}})
        assert await asyncio.to_thread(waiter.get, True, 1) == {"data": []}
        event = await asyncio.wait_for(client._client.next_notification(), 1)
        assert event.method == "skills/changed"
        assert outgoing.empty()
        release.set()
        assert await asyncio.to_thread(outgoing.get, True, 1) == {
            "id": 7,
            "result": {"decision": "accept"},
        }
    finally:
        release.set()
        incoming.put(None)
        await reader
        await client.close()


async def test_closing_shared_client_cancels_and_drains_approval() -> None:
    started, finished = asyncio.Event(), asyncio.Event()

    async def approve(method: str, params: JsonObject | None) -> JsonObject:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
        return {}

    client = SharedCodex(CodexConfig(), approve, instrument=False)
    client.transport.dispatch(
        {"id": 1, "method": "item/commandExecution/requestApproval"}
    )
    await started.wait()
    await client.close()
    assert finished.is_set()
    assert not client.transport.requests


async def test_failed_approval_does_not_fail_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outgoing: Queue[JsonObject] = Queue()

    async def approve(method: str, params: JsonObject | None) -> JsonObject:
        if params == {"threadId": "alice"}:
            raise ValueError("private failure")
        return {"decision": "decline"}

    def write(client: CodexClient, message: JsonObject) -> None:
        outgoing.put(message)

    monkeypatch.setattr(CodexClient, "_write_message", write)
    client = SharedCodex(CodexConfig(), approve, instrument=False)
    for request_id, thread_id in ((1, "alice"), (2, "bob")):
        client.transport.dispatch(
            {"id": request_id, "method": "approve", "params": {"threadId": thread_id}}
        )
    await asyncio.gather(*client.transport.requests)
    replies = {
        item["id"]: item for item in (outgoing.get_nowait(), outgoing.get_nowait())
    }
    assert replies[1] == {
        "id": 1,
        "error": {"code": -32603, "message": "Octomate could not answer the request."},
    }
    assert replies[2] == {"id": 2, "result": {"decision": "decline"}}
    await client.close()
