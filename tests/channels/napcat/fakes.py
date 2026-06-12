from __future__ import annotations

from octomate.types.json import JsonObject


class FakeNapcatResponse:
    def __init__(self, data: JsonObject | None = None) -> None:
        self._data = data or {"data": {"message_id": "msg-1"}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> JsonObject:
        return self._data


class FakeNapcatHTTP:
    def __init__(self) -> None:
        self.posts: list[tuple[str, JsonObject]] = []

    async def post(self, endpoint: str, json: JsonObject) -> FakeNapcatResponse:
        self.posts.append((endpoint, json))
        return FakeNapcatResponse()
