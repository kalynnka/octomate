import json

import httpx
import pytest

from octomate.config import NapcatChannelConfig
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.napcat import NapcatTentacle
from tests.support.channels import FakeOctomate

pytestmark = pytest.mark.usefixtures("in_memory_engine")


@pytest.mark.parametrize("previous_title", [None, "Old opening line"])
async def test_inbound_group_messages_save_the_platform_name(
    previous_title: str | None,
) -> None:
    octomate = FakeOctomate()
    channel = NapcatTentacle(
        "napcat",
        octomate,
        config=NapcatChannelConfig(
            agents=["inkling"],
            http_url="http://napcat.test",
            ws_url="ws://napcat.test",
            mention_only=True,
        ),
    )
    key = ThreadKey("napcat", "group", "2002")
    if previous_title is not None:
        await octomate.threads.ensure(key, title=previous_title)
    group_name = "Our group"
    lookups: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get_group_info":
            lookups.append(request.content)
            return httpx.Response(
                200, json={"status": "ok", "data": {"group_name": group_name}}
            )
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "user_id": json.loads(request.content).get("user_id", 42),
                    "nickname": "Bot",
                },
            },
        )

    await channel.ink.httpx.aclose()
    async with httpx.AsyncClient(
        base_url="http://napcat.test",
        transport=httpx.MockTransport(respond),
    ) as client:
        channel.ink.httpx = client
        await channel.probe()
        names = [
            ("Our group", "Stale lookup name"),
            (None, "Fetched group"),
            ("", "Fetched rename"),
            ("Renamed group", "Stale lookup name"),
        ]
        for message_id, (event_name, group_name) in enumerate(names, 1):
            await channel.ingest(
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "time": 1710000000,
                        "self_id": 42,
                        "message_id": message_id,
                        "group_id": 2002,
                        **(
                            {"group_name": event_name} if event_name is not None else {}
                        ),
                        "user_id": 3003,
                        "sender": {"user_id": 3003, "nickname": "Alice"},
                        "message": [
                            {"type": "text", "data": {"text": "Just chatting"}}
                        ],
                    }
                )
            )
            saved = await octomate.threads.get(key)
            assert saved is not None
            assert saved.title == (event_name or group_name)
            assert len(saved.messages) == message_id
        await channel.ingest(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "time": 1710000000,
                    "self_id": 42,
                    "message_id": 5,
                    "user_id": 3003,
                    "sender": {"user_id": 3003, "nickname": "Alice"},
                    "message": [
                        {"type": "text", "data": {"text": "A private message"}}
                    ],
                }
            )
        )
    assert lookups == [b'{"group_id":"2002"}'] * 2
    assert len(octomate.kicks) == 1
