from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from types import SimpleNamespace, TracebackType
from typing import cast

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection

from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import ImageData, ImageSegment, TextSegment
from octomate.tentacles.feelers.base import Feelers
from octomate.tentacles.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.feelers.oauth import PlainTextOAuthFeeler
from octomate.tentacles.feelers.output import (
    DefaultMarkdownFeeler,
    DefaultSegmentsFeeler,
    DefaultTimelineFeeler,
)
from octomate.tentacles.napcat import NapcatChromo, NapcatInk, NapcatTentacle
from octomate.tentacles.napcat.schema import NapcatOutboundMessage
from tests.channels.napcat.fakes import FakeNapcatHTTP
from tests.support.channels import drive
from tests.support.scenarios import plain_answer, play


async def test_napcat_ink_sends_group_private_and_reply_messages() -> None:
    http = FakeNapcatHTTP()
    ink = object.__new__(NapcatInk)
    ink.httpx = cast(httpx.AsyncClient, http)
    message = NapcatOutboundMessage(
        segments=[{"type": "text", "data": {"text": "hello"}}]
    )

    group_id = await ink.send_message(
        "2002",
        "group",
        [message],
        reply_to="1001",
        channel_thread_id="2002",
    )
    private_id = await ink.send_message(
        "3003", "dm", [message], channel_thread_id="3003"
    )

    assert group_id == "msg-1"
    assert private_id == "msg-1"
    assert http.posts == [
        (
            "/send_group_msg",
            {
                "group_id": "2002",
                "message": message.segments,
                "reply": "1001",
            },
        ),
        (
            "/send_private_msg",
            {"user_id": "3003", "message": message.segments},
        ),
    ]


async def test_napcat_segments_feeler_delivers_native_media(tmp_path) -> None:
    http = FakeNapcatHTTP()
    ink = object.__new__(NapcatInk)
    ink.httpx = cast(httpx.AsyncClient, http)
    feeler = DefaultSegmentsFeeler(ink=ink, chromo=NapcatChromo())
    image = tmp_path / "pic.png"
    image.write_bytes(b"image-bytes")
    address = ChannelAddress(
        channel_tentacle_id="napcat",
        chat_type="thread",
        chat_id="2002",
        user_id="3003",
        channel_thread_id="1001",
    )

    message_id = await feeler.present(
        address,
        [
            TextSegment(data={"text": "look:"}),
            ImageSegment(data=ImageData(file=str(image))),
        ],
    )

    image_b64 = base64.b64encode(b"image-bytes").decode()
    assert message_id == "msg-1"
    assert http.posts == [
        (
            "/send_group_msg",
            {
                "group_id": "2002",
                "message": [
                    {"type": "text", "data": {"text": "look:"}},
                    {"type": "image", "data": {"file": f"base64://{image_b64}"}},
                ],
                "reply": "1001",
            },
        )
    ]


async def test_napcat_segments_feeler_empty_sends_nothing() -> None:
    http = FakeNapcatHTTP()
    ink = object.__new__(NapcatInk)
    ink.httpx = cast(httpx.AsyncClient, http)
    feeler = DefaultSegmentsFeeler(ink=ink, chromo=NapcatChromo())
    address = ChannelAddress(
        channel_tentacle_id="napcat",
        chat_type="dm",
        chat_id="3003",
        user_id="3003",
    )

    message_id = await feeler.present(address, [TextSegment(data={"text": ""})])

    assert message_id is None
    assert http.posts == []


async def test_napcat_tentacle_sense_invokes_ingest() -> None:
    channel = object.__new__(NapcatTentacle)
    calls: list[str | bytes] = []

    async def ingest(raw: str | bytes) -> None:
        calls.append(raw)

    class FakeWS:
        def __aiter__(self) -> AsyncIterator[str]:
            return self._events()

        async def _events(self) -> AsyncIterator[str]:
            yield "event-1"
            yield "event-2"

    channel.ingest = ingest

    await channel.sense(cast(ClientConnection, FakeWS()))

    assert calls == ["event-1", "event-2"]


async def test_napcat_tentacle_connects_with_auth_header(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []
    channel = object.__new__(NapcatTentacle)
    channel.id = "napcat"
    channel.ws_url = "ws://napcat"
    channel.access_token = SecretStr("token")
    channel.backoff_base = 0.01
    channel.backoff_max = 0.01
    channel.backoff_factor = 2.0
    channel.ws_client = None
    channel.stop_event = None

    class FakeConnect:
        def __init__(self, url: str, additional_headers: dict[str, str] | None) -> None:
            calls.append((url, additional_headers))

        async def __aenter__(self) -> ClientConnection:
            return cast(ClientConnection, SimpleNamespace())

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

    async def sense(ws: ClientConnection) -> None:
        assert channel.stop_event is not None
        channel.stop_event.set()

    channel.sense = sense
    monkeypatch.setattr(
        "octomate.tentacles.napcat.base.connect",
        FakeConnect,
    )

    await channel.serve()

    assert calls == [
        ("ws://napcat", {"Authorization": "Bearer token"}),
    ]


async def test_napcat_context_manager_runs_serve_until_exit(monkeypatch) -> None:
    channel = object.__new__(NapcatTentacle)
    channel.id = "napcat"
    # Entering the tentacle enters the ink's context; the base ink holds no
    # resources, so a bare instance is enough.
    channel.ink = object.__new__(NapcatInk)
    channel.ws_client = None
    channel.stop_event = None
    channel.serve_task = None

    started = asyncio.Event()
    blocked = asyncio.Event()

    async def fake_probe() -> None:
        return None

    async def fake_serve() -> None:
        started.set()
        await blocked.wait()

    monkeypatch.setattr(channel, "probe", fake_probe)
    monkeypatch.setattr(channel, "serve", fake_serve)

    async with channel:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert channel.serve_task is not None
        assert not channel.serve_task.done()

    # __aexit__ cancels and joins the never-completing serve loop.
    assert channel.serve_task is None


async def test_napcat_consume_renders_plain_answer_via_default_timeline() -> None:
    http = FakeNapcatHTTP()
    ink = object.__new__(NapcatInk)
    ink.httpx = cast(httpx.AsyncClient, http)
    chromo = NapcatChromo()
    channel = object.__new__(NapcatTentacle)
    channel.id = "napcat"
    channel.ink = ink
    channel.chromo = chromo
    markdown_feeler = DefaultMarkdownFeeler(ink=ink, chromo=chromo)
    approvals = PlainTextApprovalFeeler(markdown_feeler)
    ask_questions = PlainTextAskQuestionFeeler(markdown_feeler)
    oauth = PlainTextOAuthFeeler(ink, markdown_feeler)
    channel.feelers = Feelers(
        markdown=markdown_feeler,
        timeline=DefaultTimelineFeeler(
            ink=ink,
            chromo=chromo,
            ask_questions=ask_questions,
            approvals=approvals,
            oauth=oauth,
            deferred_actions=DeferredActionManager(),
        ),
        segments=DefaultSegmentsFeeler(ink=ink, chromo=chromo),
        approvals=approvals,
        ask_questions=ask_questions,
        oauth=oauth,
    )
    address = ChannelAddress(
        channel_tentacle_id="napcat",
        chat_type="dm",
        chat_id="3003",
        user_id="3003",
    )

    message_id = await drive(
        channel, address, play(plain_answer("hello from octomate"))
    )

    assert message_id == "msg-1"
    assert http.posts == [
        (
            "/send_private_msg",
            {
                "user_id": "3003",
                "message": [{"type": "text", "data": {"text": "hello from octomate"}}],
            },
        )
    ]
