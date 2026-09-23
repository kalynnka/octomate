from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import JsonValue, ValidationError

from octomate.tentacles.napcat.ink import NapcatInk
from octomate.tentacles.napcat.schema import NapcatOutboundMessage


@pytest.fixture
async def napcat_api() -> AsyncIterator[
    tuple[NapcatInk, list[httpx.Response], list[httpx.Request]]
]:
    responses: list[httpx.Response] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async with httpx.AsyncClient(
        base_url="http://napcat", transport=httpx.MockTransport(respond)
    ) as client:
        ink = object.__new__(NapcatInk)
        ink.httpx = client
        yield ink, responses, requests


async def test_inspect_normalizes_login_identity_and_ignores_unknown_fields(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
) -> None:
    ink, responses, requests = napcat_api
    responses.append(
        httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": 42, "nickname": "Octomate", "future": True},
            },
        )
    )

    profile = await ink.inspect()

    assert profile.channel_user_id == "42"
    assert profile.name == "Octomate"
    assert [request.url.path for request in requests] == ["/get_login_info"]


@pytest.mark.parametrize(
    ("data", "expected_id"),
    [({"user_id": 123, "nickname": "Alice"}, "123"), ({"nick": "Alice"}, "42")],
)
async def test_profile_preserves_identity_and_name_normalization(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
    data: JsonValue,
    expected_id: str,
) -> None:
    ink, responses, _ = napcat_api
    responses.append(
        httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})
    )

    profile = await ink.get_user_profile("42")

    assert profile.channel_user_id == expected_id
    assert profile.name == "Alice"


@pytest.mark.parametrize(
    "body",
    [
        '{"status": "ok", "data": []}',
        '{"status": "ok", "data": {"user_id": [42]}}',
        "[]",
        "{",
    ],
)
async def test_invalid_profile_response_keeps_the_existing_identity_fallback(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]], body: str
) -> None:
    ink, responses, _ = napcat_api
    responses.append(httpx.Response(200, text=body))

    profile = await ink.get_user_profile("42")

    assert profile.channel_user_id == "42"
    assert profile.name == "42"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"url": "https://image.test/pic.png"}, "https://image.test/pic.png"),
        ({}, None),
    ],
)
async def test_image_response_url_is_optional(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
    data: JsonValue,
    expected: str | None,
) -> None:
    ink, responses, _ = napcat_api
    responses.append(
        httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})
    )

    assert await ink.get_image_url("image-key") == expected


@pytest.mark.parametrize("body", [{"status": "ok", "data": None}, {"status": "ok"}])
async def test_image_response_requires_data(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
    body: JsonValue,
) -> None:
    ink, responses, _ = napcat_api
    responses.append(httpx.Response(200, json=body))

    with pytest.raises(ValueError, match="returned no data"):
        await ink.get_image_url("image-key")


@pytest.mark.parametrize("url", [42, [], {}])
async def test_invalid_image_urls_fail_at_the_response_boundary(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
    url: JsonValue,
) -> None:
    ink, responses, _ = napcat_api
    responses.append(
        httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"url": url}})
    )

    with pytest.raises(ValidationError):
        await ink.get_image_url("image-key")


@pytest.mark.parametrize(
    ("first_data", "error", "match"),
    [
        (None, ValueError, "returned no data"),
        ({}, ValueError, "returned no message_id"),
        ({"message_id": None}, ValueError, "returned no message_id"),
        ({"message_id": []}, ValidationError, "message_id"),
        ({"message_id": True}, ValidationError, "message_id"),
    ],
)
async def test_send_rejects_missing_or_invalid_ids_before_sending_more_messages(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
    first_data: JsonValue,
    error: type[Exception],
    match: str,
) -> None:
    ink, responses, requests = napcat_api
    responses.extend(
        httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})
        for data in [first_data, {"message_id": 123}, {"message_id": 456}]
    )
    message = NapcatOutboundMessage(segments=[{"type": "text", "data": {"text": "hi"}}])

    with pytest.raises(error, match=match):
        await ink.send_message(
            "42", "dm", [message, message, message], channel_thread_id="42"
        )

    assert len(requests) == 1


async def test_send_returns_the_first_id_as_text(
    napcat_api: tuple[NapcatInk, list[httpx.Response], list[httpx.Request]],
) -> None:
    ink, responses, requests = napcat_api
    responses.extend(
        httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": message_id}}
        )
        for message_id in [123, 456]
    )
    message = NapcatOutboundMessage(segments=[{"type": "text", "data": {"text": "hi"}}])

    result = await ink.send_message(
        "42", "dm", [message, message], channel_thread_id="42"
    )

    assert result == "123"
    assert len(requests) == 2
