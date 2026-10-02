"""The Ink execution boundary preserves native request and outcome semantics."""

from pathlib import Path

import httpx
import pytest
from octomate_protocol.deepseek import ClientRequest
from pydantic import HttpUrl, TypeAdapter, ValidationError
from pydantic_ai.exceptions import AgentRunError

from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.ink import DeepseekInk
from octomate.tentacles.deepseek.wire import CommandError, CommandSuccess
from octomate.types.json import JsonObject, JsonValue

BASE_URL = "http://127.0.0.1:3080/"


@pytest.fixture
def replies() -> JsonObject:
    payload = TypeAdapter(JsonObject).validate_json(
        (Path(__file__).parent / "fixtures/deepseek_command_results.json").read_text()
    )
    replies = payload["replies"]
    assert isinstance(replies, dict)
    return replies


@pytest.mark.parametrize(
    "case", ["success", "silent", "error", "unmatched", "remote_error"]
)
async def test_command_execution_preserves_arguments_and_native_outcomes(
    replies: JsonObject, case: str
) -> None:
    requests: list[ClientRequest] = []
    line = '/goal \t "raw argument"\n--flag=✓  '

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/commands/execute"
        message = ClientRequest.model_validate_json(request.content)
        requests.append(message)
        assert message.payload == {
            "args": {
                "agentId": "session-1",
                "line": line,
                "submittedAttachments": [],
            }
        }
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": message.rpc_id,
                "result": replies[case],
            },
        )

    client = DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)),
    )
    async with DeepseekInk(client) as ink:
        if case == "remote_error":
            with pytest.raises(
                AgentRunError, match=r"Session is not loaded.*gateway/lookup-not-found"
            ):
                await ink.execute_command("session-1", line)
            assert len(requests) == 1
            return
        execution = await ink.execute_command("session-1", line)

    assert len(requests) == 1
    if case == "unmatched":
        assert execution is None
        return
    assert execution is not None
    if case == "error":
        assert execution.command_id == "command-3"
        assert isinstance(execution.result, CommandError)
        assert execution.result.text == "Unknown permission preset"
        return
    assert isinstance(execution.result, CommandSuccess)
    assert execution.command_id == ("command-1" if case == "success" else "command-2")
    assert execution.result.text == (
        "  Goal accepted.\n" if case == "success" else None
    )
    assert execution.result.source_event_seq == (17 if case == "success" else None)


@pytest.mark.parametrize("failure", ["connection-refused", "http-401"])
async def test_transport_failure_is_not_an_unmatched_or_command_error_result(
    failure: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "connection-refused":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(401)

    async with DeepseekInk(
        DeepseekApiClient(
            HttpUrl(BASE_URL),
            httpx.AsyncClient(
                base_url=BASE_URL, transport=httpx.MockTransport(respond)
            ),
        )
    ) as ink:
        with pytest.raises(AgentRunError, match=failure):
            await ink.execute_command("session-1", "/permission workspace-write")


@pytest.mark.parametrize(
    "value",
    [
        {"commandId": "cmd-1"},
        {"commandId": "cmd-1", "result": {"kind": "unknown"}},
        {"commandId": "cmd-1", "result": {"kind": "error"}},
    ],
)
async def test_incomplete_or_unknown_outcomes_fail_validation(value: JsonValue) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        message = ClientRequest.model_validate_json(request.content)
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": message.rpc_id,
                "result": {"ok": True, "value": value},
            },
        )

    async with DeepseekInk(
        DeepseekApiClient(
            HttpUrl(BASE_URL),
            httpx.AsyncClient(
                base_url=BASE_URL, transport=httpx.MockTransport(respond)
            ),
        )
    ) as ink:
        with pytest.raises(ValidationError):
            await ink.execute_command("session-1", "/permission workspace-write")
