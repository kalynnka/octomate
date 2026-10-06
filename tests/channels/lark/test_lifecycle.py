"""Lark's SDK receiver belongs to the channel's shutdown lifecycle."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import lark_oapi.ws.client as ws_mod
import pytest
from pydantic import SecretStr
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from octomate import Octomate
from octomate.config import LarkChannelConfig
from octomate.tentacles.lark import LarkTentacle


@pytest.fixture
async def websocket_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[LarkTentacle, asyncio.Event, list[asyncio.Task[None]]]]:
    channel = LarkTentacle(
        "lark",
        Octomate(),
        config=LarkChannelConfig(
            app_id="test-app", app_secret=SecretStr("secret"), agents=["inkling"]
        ),
    )
    receiving = asyncio.Event()
    closed = asyncio.Event()
    socket = AsyncMock()

    async def receive() -> bytes:
        receiving.set()
        await closed.wait()
        raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), False)

    socket.recv.side_effect = receive
    socket.close.side_effect = closed.set
    monkeypatch.setattr(ws_mod, "loop", asyncio.get_running_loop())
    monkeypatch.setattr(ws_mod.websockets, "connect", AsyncMock(return_value=socket))
    monkeypatch.setattr(
        channel.ws_client,
        "_get_conn_url",
        lambda: "wss://example.test/ws?device_id=test-device&service_id=1",
    )
    monkeypatch.setattr(channel, "probe", AsyncMock())
    receive_tasks: list[asyncio.Task[None]] = []
    sdk_receive = channel.ws_client._receive_message_loop

    async def tracked_receive() -> None:
        task = asyncio.current_task()
        assert task is not None
        receive_tasks.append(task)
        await sdk_receive()

    monkeypatch.setattr(channel.ws_client, "_receive_message_loop", tracked_receive)
    try:
        yield channel, receiving, receive_tasks
    finally:
        # The SDK's cache owns a housekeeping task even without a connection.
        tasks = [*receive_tasks, channel.ws_client._cache._cron]
        if channel.ping_task is not None:
            tasks.append(channel.ping_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if channel.ink.http is not None:
            await channel.ink.__aexit__()


@pytest.mark.parametrize("wait_for_receiver", [False, True])
async def test_shutdown_joins_receiver_before_closing_socket(
    websocket_channel: tuple[LarkTentacle, asyncio.Event, list[asyncio.Task[None]]],
    wait_for_receiver: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, receiving, receivers = websocket_channel
    async with asyncio.timeout(2), channel:
        if wait_for_receiver:
            await receiving.wait()

    assert channel.ping_task is None
    assert channel.ink.http is None
    assert receivers
    assert all(task.cancelled() for task in receivers)
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


async def test_shutdown_cancels_a_reconnecting_receiver(
    websocket_channel: tuple[LarkTentacle, asyncio.Event, list[asyncio.Task[None]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, receiving, receivers = websocket_channel
    reconnecting = asyncio.Event()

    async def reconnect() -> None:
        reconnecting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(channel.ws_client, "_reconnect", reconnect)
    async with asyncio.timeout(2), channel:
        await receiving.wait()
        await channel.ws_client._disconnect()
        await reconnecting.wait()

    assert all(task.cancelled() for task in receivers)
    assert channel.ink.http is None


async def test_shutdown_retrieves_receiver_failure_and_closes_http(
    websocket_channel: tuple[LarkTentacle, asyncio.Event, list[asyncio.Task[None]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, receiving, receivers = websocket_channel
    failed = asyncio.Event()

    async def reconnect() -> None:
        raise RuntimeError("reconnect failed")

    monkeypatch.setattr(channel.ws_client, "_reconnect", reconnect)
    async with asyncio.timeout(2):
        await channel.__aenter__()
        await receiving.wait()
        receivers[0].add_done_callback(lambda _: failed.set())
        await channel.ws_client._disconnect()
        await failed.wait()
        with pytest.raises(RuntimeError, match="reconnect failed"):
            await channel.__aexit__()

    assert channel.ping_task is None
    assert channel.ink.http is None
