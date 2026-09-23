"""Configuration preservation and dsh transport validation at JSON boundaries."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from octomate_cli.streaming import deepseek
from octomate_cli.tentacles.claude.config import load_settings, write_settings
from pydantic import ValidationError
from typer import BadParameter


def test_claude_settings_preserve_unknown_fields_and_explicit_nulls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    document = {
        "theme": None,
        "mcpServers": {
            "stdio": {"command": "tool", "args": ["--flag"], "env": {"KEY": "值"}},
            "http": {"type": "http", "url": "https://example.com", "future": None},
        },
        "projects": {"/work": {"trust": True, "history": [None, {"future": 1}]}},
    }
    path.write_text(json.dumps(document))

    settings = load_settings(path)
    assert settings.mcp_servers["http"].url == "https://example.com"
    write_settings(path, settings)

    assert json.loads(path.read_bytes()) == document


@pytest.mark.parametrize(
    "content", ["[]", '{"mcpServers": []}', '{"projects": {"/work": null}}']
)
def test_claude_settings_reject_invalid_containers_without_rewriting(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(content)
    with pytest.raises(BadParameter, match="Invalid MCP settings"):
        load_settings(path)
    assert path.read_text() == content


@pytest.mark.parametrize("ok", [True, False])
def test_dsh_rpc_validates_envelope_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch, ok: bool
) -> None:
    monkeypatch.delenv("DSH_LAUNCH_TOKEN", raising=False)
    client = deepseek.DshHistoryClient("http://localhost:3080")
    value = {"records": [{"future": None}], "extension": {"text": "你好"}}
    result = (
        {"ok": True, "value": value}
        if ok
        else {"ok": False, "error": {"code": "bad_request", "message": "bad page"}}
    )
    opener = MagicMock(
        return_value=BytesIO(
            json.dumps(
                {"type": "server-response", "rpcId": "rpc-1", "result": result}
            ).encode()
        )
    )
    monkeypatch.setattr(client.opener, "open", opener)

    if ok:
        assert client.rpc("session/page", {"request": {}}) == value
    else:
        with pytest.raises(deepseek.DshCompatibilityError, match="bad page"):
            client.rpc("session/page", {"request": {}})
    request = opener.call_args.args[0]
    body = json.loads(request.data)
    assert body["type"] == "client-request"
    assert body["rpcId"]
    assert body["method"] == "session/page"
    assert body["payload"] == {"args": {"request": {}}}


def test_dsh_snapshot_preserves_unknown_history_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DSH_LAUNCH_TOKEN", raising=False)
    client = deepseek.DshHistoryClient("http://localhost:3080")
    value = {"type": "snapshot", "cursor": 7, "records": [{"future": None}]}
    socket = MagicMock()
    socket.recv.return_value = json.dumps(
        {"type": "item", "streamId": "session", "value": value}
    )
    connection = MagicMock()
    connection.return_value.__enter__.return_value = socket
    monkeypatch.setattr(deepseek, "connect_sync", connection)

    assert client.snapshot("session") == value
    request = json.loads(socket.send.call_args.args[0])
    assert request == {
        "type": "open",
        "streamId": "session",
        "endpoint": "session/follow",
        "payload": {
            "args": {
                "request": {
                    "address": {"kind": "session", "sessionId": "session"},
                    "maxMessages": deepseek.PAGE_MESSAGES,
                }
            }
        },
    }
    socket.recv.return_value = '{"type":"item","value":{}}'
    with pytest.raises(ValidationError, match="streamId"):
        client.snapshot("session")
