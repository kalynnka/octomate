from threading import get_ident

import lark_oapi as lark
import pytest
from lark_oapi.core.cache.local_cache import LocalCache
from lark_oapi.core.exception import ObtainAccessTokenException
from lark_oapi.core.http import Transport
from lark_oapi.core.model import BaseRequest, Config, RawResponse, RequestOption
from lark_oapi.core.token import TokenManager
from pydantic import SecretStr, ValidationError

from octomate.tentacles.lark.ink import LarkInk

type SDKCall = tuple[BaseRequest, RequestOption | None, int]


def response(body: bytes) -> RawResponse:
    result = RawResponse()
    result.status_code = 200
    result.headers = {"Content-Type": "application/json"}
    result.content = body
    return result


@pytest.fixture
def sdk_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[RawResponse], list[SDKCall]]:
    responses = [
        response(b'{"code":0,"tenant_access_token":"test-token","expire":7200}')
    ]
    calls: list[SDKCall] = []

    def execute(
        config: Config, request: BaseRequest, option: RequestOption | None = None
    ) -> RawResponse:
        calls.append((request, option, get_ident()))
        return responses.pop(0)

    monkeypatch.setattr(TokenManager, "cache", LocalCache())
    monkeypatch.setattr(Transport, "execute", execute)
    return responses, calls


async def test_inspect_uses_sdk_auth_and_token_cache_off_the_event_loop(
    sdk_transport: tuple[list[RawResponse], list[SDKCall]],
) -> None:
    responses, calls = sdk_transport
    body = (
        b'{"code":0,"bot":{"open_id":"ou_bot","app_name":"Octomate",'
        b'"avatar_url":"https://image.test/bot.png","future_field":true}}'
    )
    responses.extend([response(body), response(body)])
    ink = LarkInk("test-app", SecretStr("test-secret"))
    event_loop_thread = get_ident()

    for _ in range(2):
        profile = await ink.inspect()
        assert profile.channel_user_id == "ou_bot"
        assert profile.name == "Octomate"
        assert profile.avatar_url == "https://image.test/bot.png"

    assert [request.uri for request, _, _ in calls] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/bot/v3/info",
        "/open-apis/bot/v3/info",
    ]
    assert all(thread != event_loop_thread for _, _, thread in calls)
    for request, option, _ in calls[1:]:
        assert request.http_method == lark.HttpMethod.GET
        assert request.token_types == {lark.AccessTokenType.TENANT}
        assert option is not None
        assert option.tenant_access_token == "test-token"


async def test_inspect_surfaces_the_sdk_authentication_error(
    sdk_transport: tuple[list[RawResponse], list[SDKCall]],
) -> None:
    responses, calls = sdk_transport
    responses[:] = [response(b'{"code":10003,"msg":"invalid app secret"}')]
    ink = LarkInk("test-app", SecretStr("test-secret"))

    with pytest.raises(ObtainAccessTokenException):
        await ink.inspect()

    assert len(calls) == 1


async def test_inspect_rejects_an_api_error_even_with_bot_data(
    sdk_transport: tuple[list[RawResponse], list[SDKCall]],
) -> None:
    responses, _ = sdk_transport
    responses.append(
        response(b'{"code":999,"msg":"denied","bot":{"open_id":"ou_bot"}}')
    )
    ink = LarkInk("test-app", SecretStr("test-secret"))

    with pytest.raises(RuntimeError, match="inspect failed: 999 denied"):
        await ink.inspect()


@pytest.mark.parametrize(
    "body",
    [
        b'{"code":0}',
        b'{"code":0,"bot":null}',
        b'{"code":0,"bot":[]}',
        b'{"code":0,"bot":{}}',
        b'{"code":0,"bot":{"open_id":""}}',
        b'{"code":0,"bot":{"open_id":42}}',
    ],
)
async def test_inspect_rejects_missing_or_malformed_bot_identity(
    sdk_transport: tuple[list[RawResponse], list[SDKCall]], body: bytes
) -> None:
    responses, _ = sdk_transport
    responses.append(response(body))
    ink = LarkInk("test-app", SecretStr("test-secret"))

    with pytest.raises(ValidationError):
        await ink.inspect()
