"""Run DSH's connector authorization without handling its OAuth credentials."""

import json
import os
import time
import urllib.request
import webbrowser
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import typer
from octomate_protocol.deepseek import RemoteItem, remote_message_adapter
from pydantic import AnyHttpUrl, BaseModel, Field, TypeAdapter
from uuid_utils.compat import uuid7
from websockets.sync.client import connect

from octomate_cli.config import cli_settings
from octomate_cli.streaming.deepseek import DshCompatibilityError, DshHistoryClient


class Authorization(BaseModel):
    """DSH's retained authorization attempt, including its browser handoff."""

    id: str
    phase: Literal[
        "starting",
        "waiting-browser",
        "exchanging",
        "committing",
        "authorized",
        "cancelled",
        "failed",
    ]
    authorization_url: AnyHttpUrl | None = Field(default=None, alias="authorizationUrl")


class ManagedServer(BaseModel):
    name: str
    authorization: Authorization | None = None


managed_servers = TypeAdapter(list[ManagedServer])
REGISTRATION_TIMEOUT = 15
AUTHORIZATION_TIMEOUT = 600


def login(url: str | None = None) -> None:
    """Start DSH's Octomate account operation and follow the native attempt."""
    launch = (
        url
        or os.environ.get("DSH_API_URL")
        or cli_settings().deepseek.url
        or "http://127.0.0.1:3080"
    )
    client = DshHistoryClient(launch)
    request = urllib.request.Request(f"{client.url}/api/remote.mux")
    client.cookies.add_cookie_header(request)
    parts = urlsplit(client.url)
    socket_url = urlunsplit(
        parts._replace(
            scheme="wss" if parts.scheme == "https" else "ws", path="/api/remote.mux"
        )
    )
    stream_id = str(uuid7())
    attempt: str | None = None
    opened = False
    deadline = time.monotonic() + REGISTRATION_TIMEOUT
    with connect(
        socket_url,
        additional_headers={"Cookie": request.get_header("Cookie", "")},
        open_timeout=10,
    ) as socket:
        socket.send(
            json.dumps(
                {
                    "type": "open",
                    "streamId": stream_id,
                    "endpoint": "mcpManager/watch",
                    "payload": {"args": {}},
                }
            )
        )
        while True:
            message = remote_message_adapter.validate_json(
                socket.recv(timeout=max(0, deadline - time.monotonic()))
            )
            if not isinstance(message, RemoteItem) or message.stream_id != stream_id:
                raise DshCompatibilityError(
                    "DSH could not watch MCP authorization. Update DSH and Octomate together."
                )
            servers = managed_servers.validate_python(message.value)
            server = next(
                (entry for entry in servers if entry.name == "octomate"), None
            )
            if server is None:
                continue
            if attempt is None:
                result = client.call(
                    "connectorAccounts/start",
                    {"key": "mcp:octomate", "operation": "connect"},
                )
                if not isinstance(result, str):
                    raise DshCompatibilityError(
                        "DSH did not return an authorization attempt."
                    )
                attempt = result
                deadline = time.monotonic() + AUTHORIZATION_TIMEOUT
                continue
            authorization = server.authorization
            if authorization is None or authorization.id != attempt:
                continue
            if authorization.phase == "authorized":
                typer.secho(
                    "DSH OAuth authorization completed; credentials are stored by DSH.",
                    fg=typer.colors.GREEN,
                    err=True,
                )
                return
            if authorization.phase in {"failed", "cancelled"}:
                raise DshCompatibilityError(
                    f"DSH OAuth authorization {authorization.phase}. Run `octomate deepseek mcp login` to try again."
                )
            if authorization.authorization_url and not opened:
                typer.secho(
                    "Authorize DSH in your browser:", fg=typer.colors.CYAN, err=True
                )
                typer.echo(str(authorization.authorization_url), err=True)
                webbrowser.open(str(authorization.authorization_url))
                opened = True
