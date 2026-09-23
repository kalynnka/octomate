from __future__ import annotations

import httpx

from octomate.types.json import JsonObject


class FakeNapcatHTTP:
    def __init__(self) -> None:
        self.posts: list[tuple[str, JsonObject]] = []

    async def post(self, endpoint: str, json: JsonObject) -> httpx.Response:
        self.posts.append((endpoint, json))
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"message_id": "msg-1"}},
            request=httpx.Request("POST", f"http://napcat{endpoint}"),
        )
