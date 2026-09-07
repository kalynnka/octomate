"""Package versions can differ while the stream wire protocol remains compatible."""

import pytest
from octomate_cli.tentacles.claude import CLAUDE_STREAM_PATH
from octomate_protocol.stream import StreamHello, StreamWelcome, server_message_adapter

from tests.agent.test_claude_stream import AUTH, hello_json, stream_client


@pytest.mark.usefixtures("in_memory_engine")
@pytest.mark.parametrize("client_version", ["0.0.1", "0.7.2", "2.0.0"])
def test_stream_accepts_different_package_versions(client_version: str) -> None:
    client, _ = stream_client()
    hello = StreamHello.model_validate_json(hello_json())
    hello.client_version = client_version
    with (
        client,
        client.websocket_connect(CLAUDE_STREAM_PATH, headers=AUTH) as websocket,
    ):
        websocket.send_text(hello.model_dump_json())
        welcome = server_message_adapter.validate_json(websocket.receive_text())
        assert isinstance(welcome, StreamWelcome)
