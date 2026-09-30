"""The shared SDK transport must keep reading while a human is deciding."""

import asyncio
from queue import Queue
from unittest.mock import AsyncMock

import pytest
from openai_codex import CodexConfig
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.errors import TransportClosedError
from openai_codex.models import InitializeResponse

from octomate.tentacles.codex.client import SharedCodex
from octomate.types.json import JsonObject


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
