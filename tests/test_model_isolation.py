"""Unit tests cannot accidentally reach a billable model provider."""

import socket

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pytest_socket import SocketConnectBlockedError


async def test_model_request_is_refused_before_transport() -> None:
    model = OpenAIChatModel("gpt-5", provider=OpenAIProvider(api_key="synthetic"))
    with pytest.raises(RuntimeError, match="Model requests are not allowed"):
        await Agent(model).run("This must never reach a provider")


def test_external_socket_is_refused() -> None:
    with socket.socket() as connection:
        with (
            pytest.warns(UserWarning, match="A test tried to use socket"),
            pytest.raises(SocketConnectBlockedError),
        ):
            connection.connect(("203.0.113.1", 443))
