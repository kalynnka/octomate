"""CLI OAuth over the real server routes, with an actual loopback callback."""

import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread
from typing import NoReturn
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from octomate_cli import auth as cli_auth
from octomate_cli import emit
from octomate_cli.config import CLISettings
from octomate_cli.main import app as cli
from octomate_cli.streaming import deepseek as deepseek_tail
from octomate_cli.streaming import files as file_tail
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from octomate.auth import auth_router
from octomate.authorization import discovery_router, router
from octomate.dependencies import auth_manager
from octomate.managers.auth import AuthManager
from octomate.schemas.base import sqlalchemy_materia
from tests.support.users import auth_config

ORIGIN = "https://octomate.test"
PASSWORD = "Strong-password1!"


@pytest.fixture(autouse=True)
def store_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_auth, "user_config_path", lambda: tmp_path / "cli.toml")
    monkeypatch.setattr(cli_auth, "LOGIN_TIMEOUT", 5)
    monkeypatch.setattr(
        cli_auth, "cli_settings", lambda: CLISettings.model_construct(url=ORIGIN)
    )


@pytest.fixture
async def auth(in_memory_engine: AsyncEngine) -> AuthManager:
    config = auth_config()
    config.oauth_issuer_url = AnyHttpUrl(ORIGIN)
    manager = AuthManager(config)
    await manager.register(
        "alice", SecretStr(PASSWORD), await manager.invite(), name="Alice"
    )
    return manager


@pytest.fixture
def server(auth: AuthManager, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    server = FastAPI()
    server.include_router(auth_router)
    server.include_router(router)
    server.include_router(discovery_router)
    server.dependency_overrides[auth_manager] = lambda: auth
    real_client = httpx2.Client

    def request(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url).startswith(ORIGIN + "/")
        with TestClient(server, base_url=ORIGIN) as client:
            return client.request(
                request.method,
                str(request.url),
                headers=request.headers,
                content=request.content,
                follow_redirects=False,
            )

    def oauth_http(*, timeout: float) -> httpx2.Client:
        return real_client(transport=httpx2.MockTransport(request), timeout=timeout)

    monkeypatch.setattr(cli_auth.httpx2, "Client", oauth_http)
    return server


@pytest.mark.parametrize("approve", [True, False])
async def test_login_callback_refresh_and_logout(
    server: FastAPI,
    auth: AuthManager,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    approve: bool,
) -> None:
    threads: list[Thread] = []
    failures: list[BaseException] = []

    def browse(url: str) -> bool:
        def consent() -> None:
            try:
                with (
                    sqlalchemy_materia(),
                    TestClient(server, base_url=ORIGIN) as browser,
                ):
                    login = browser.post(
                        "/api/auth/login",
                        json={"username": "alice", "password": PASSWORD},
                        headers={"X-Octomate-Request": "1"},
                    )
                    assert login.status_code == 204
                    user_id = browser.get("/api/auth/me").json()["id"]
                    authorize = browser.get(url, follow_redirects=False)
                    assert authorize.status_code == 302
                    ticket = parse_qs(urlsplit(authorize.headers["location"]).fragment)[
                        "ticket"
                    ][0]
                    decision = browser.post(
                        "/oauth/consent/decide",
                        json={
                            "ticket": ticket,
                            "expected_user_id": user_id,
                            "approve": approve,
                        },
                        headers={"X-Octomate-Request": "1"},
                    )
                    assert decision.status_code == 200
                    redirect = decision.json()["redirect_uri"]
                # urllib bypasses the patched httpx factory and exercises the listener.
                parts = urlsplit(redirect)
                query = parse_qs(parts.query)
                for key, value in [("state", "wrong"), ("iss", "https://other.test/")]:
                    forged = {name: values[0] for name, values in query.items()}
                    forged[key] = value
                    with pytest.raises(urllib.error.HTTPError) as rejected:
                        urllib.request.urlopen(
                            parts._replace(query=urlencode(forged)).geturl(), timeout=5
                        )
                    assert rejected.value.code == 400
                with urllib.request.urlopen(redirect, timeout=5) as response:
                    assert response.status == 200
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=consent, daemon=True)
        threads.append(thread)
        thread.start()
        return True

    monkeypatch.setattr(cli_auth.webbrowser, "open", browse)
    if not approve:
        with pytest.raises(cli_auth.LoginRequired, match="declined"):
            await asyncio.to_thread(cli_auth.authorize, ORIGIN)
        assert not cli_auth.LoginStore(ORIGIN).path.exists()
    else:
        await asyncio.to_thread(cli_auth.authorize, ORIGIN)
        store = cli_auth.LoginStore(ORIGIN)
        session = store.read()
        assert session is not None
        assert session.scope == "acp hooks"
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700
        assert session.access_token.get_secret_value() not in repr(session)
        assert session.refresh_token.get_secret_value() not in repr(session)
        assert (
            await auth.authenticate_oauth_token(
                session.access_token, resource=f"{ORIGIN}/api", scope="hooks"
            )
        ) is not None
        original_refresh = session.refresh_token
        with store.lock():
            session.expires_at = 0
            store.write(session)
        bearer = await asyncio.to_thread(store.bearer)
        assert bearer is not None
        rotated = store.read()
        assert rotated is not None
        assert rotated.refresh_token != original_refresh
        assert (
            await auth.authenticate_oauth_token(
                bearer, resource=f"{ORIGIN}/api", scope="acp"
            )
        ) is not None
        await asyncio.to_thread(cli_auth.logout, ORIGIN)
        assert not store.path.exists()
        assert (
            await auth.authenticate_oauth_token(
                bearer, resource=f"{ORIGIN}/api", scope="acp"
            )
        ) is None
        output = capsys.readouterr()
        for secret in [
            session.access_token,
            original_refresh,
            bearer,
            rotated.refresh_token,
        ]:
            assert secret.get_secret_value() not in output.out + output.err
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert failures == []


def saved_login(*, expires_at: float = 0, server: str = ORIGIN) -> cli_auth.LoginStore:
    store = cli_auth.LoginStore(server)
    metadata = cli_auth.OAuthMetadata(
        issuer=f"{server}/",
        authorization_endpoint=f"{server}/oauth/authorize",
        token_endpoint=f"{server}/oauth/token",
        registration_endpoint=f"{server}/oauth/register",
        revocation_endpoint=f"{server}/oauth/revoke",
        code_challenge_methods_supported=["S256"],
    )
    with store.lock():
        store.write(
            cli_auth.LoginSession(
                issuer=server,
                client_id="test-client",
                metadata=metadata,
                access_token=SecretStr("old-access"),
                refresh_token=SecretStr("old-refresh"),
                expires_at=expires_at,
                scope="hooks acp",
            )
        )
    return store


def test_refresh_is_serialized_across_processes() -> None:
    requests: list[bytes] = []

    class Refresh(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            time.sleep(0.05)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "hooks acp",
                    }
                ).encode()
            )

        def log_message(self, format: str, *args: str | int) -> None:
            return

    with HTTPServer(("127.0.0.1", 0), Refresh) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        store = saved_login(server=url)
        script = """
import sys
from pathlib import Path
from octomate_cli import auth
from pydantic import SecretStr
auth.user_config_path = lambda: Path(sys.argv[2])
assert auth.bearer(sys.argv[1], None) == SecretStr("new-access")
assert "octomate" not in sys.modules
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, url, str(cli_auth.user_config_path())],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        try:
            for process in processes:
                output, error = process.communicate(timeout=15)
                assert process.returncode == 0, error.decode()
                assert output == b""
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            server.shutdown()
            thread.join()
        assert len(requests) == 1
        assert parse_qs(requests[0].decode())["refresh_token"] == ["old-refresh"]
        stored = store.read()
        assert stored is not None
        assert stored.refresh_token == SecretStr("new-refresh")


def test_credentials_are_origin_bound_and_api_keys_take_precedence() -> None:
    saved_login(expires_at=time.time() + 3600)
    assert cli_auth.bearer(f"{ORIGIN}/hooks/claude", None) == SecretStr("old-access")
    assert cli_auth.bearer(
        "wss://octomate.test/hooks/claude/stream", None
    ) == SecretStr("old-access")
    assert cli_auth.bearer("https://other.test/hooks/claude", None) is None
    assert cli_auth.bearer(f"{ORIGIN}/hooks/claude", "runtime-key") == SecretStr(
        "runtime-key"
    )


def test_expired_grant_requires_login_without_replacing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = saved_login()
    before = store.path.read_bytes()
    real_client = httpx2.Client

    def client(*, timeout: float) -> httpx2.Client:
        return real_client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(400, json={"error": "invalid_grant"})
            ),
            timeout=timeout,
        )

    monkeypatch.setattr(cli_auth.httpx2, "Client", client)
    with pytest.raises(cli_auth.LoginRequired, match="expired or was revoked"):
        store.bearer()
    assert store.path.read_bytes() == before


def test_emit_uses_saved_cli_login(monkeypatch: pytest.MonkeyPatch) -> None:
    saved_login(expires_at=time.time() + 3600)
    monkeypatch.setattr(
        emit.sys,
        "stdin",
        StringIO(json.dumps({"hook_event_name": "SessionEnd", "session_id": "s1"})),
    )

    def request(request: urllib.request.Request, *, timeout: float) -> NoReturn:
        assert request.get_header("Authorization") == "Bearer old-access"
        raise OSError("checked credential")

    monkeypatch.setattr(emit.urllib.request, "urlopen", request)
    assert emit.main(f"{ORIGIN}/hooks/claude", None) == 1


def test_cli_commands_are_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli_auth, "authorize", lambda url, *, open_browser: calls.append(url)
    )
    result = CliRunner().invoke(cli, ["login", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert calls == [ORIGIN]
    result = CliRunner().invoke(cli, ["logout"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.test",
        "https://user:password@octomate.test",
        "ftp://octomate.test",
    ],
)
def test_oauth_refuses_insecure_or_credentialed_origins(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        cli_auth.LoginStore(url)


async def test_login_timeout_leaves_no_credentials(
    server: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_auth, "LOGIN_TIMEOUT", 0)
    with pytest.raises(cli_auth.LoginRequired, match="timed out"):
        await asyncio.to_thread(cli_auth.authorize, ORIGIN, open_browser=False)
    assert not cli_auth.LoginStore(ORIGIN).path.exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://other.test/token",
        "http://octomate.test/token",
        f"{ORIGIN}/token?secret=x",
    ],
)
def test_stored_metadata_cannot_send_refresh_credentials_elsewhere(
    endpoint: str,
) -> None:
    store = saved_login()
    with store.lock():
        session = store.read()
        assert session is not None
        session.metadata.token_endpoint = endpoint
        store.write(session)
    with pytest.raises(ValueError, match="OAuth"):
        store.bearer()


@pytest.mark.parametrize("agent", ["files", "deepseek"])
async def test_tails_resolve_credentials_on_every_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    store = saved_login(expires_at=time.time() + 3600)
    received: list[str] = []

    def connect(url: str, *, additional_headers: dict[str, str]) -> NoReturn:
        received.append(additional_headers["Authorization"])
        raise RuntimeError("checked credential")

    tail = file_tail if agent == "files" else deepseek_tail
    monkeypatch.setattr(tail, "connect", connect)
    monkeypatch.setattr(deepseek_tail, "DshHistoryClient", lambda url: None)
    for access in ("first-access", "second-access"):
        with store.lock():
            session = store.read()
            assert session is not None
            session.access_token = SecretStr(access)
            store.write(session)
        stream = (
            file_tail.stream_session(
                "wss://octomate.test/hooks/claude/stream",
                "s1",
                tmp_path / "transcript",
                "/",
                None,
            )
            if agent == "files"
            else deepseek_tail.stream_session(
                "wss://octomate.test/hooks/deepseek/stream",
                "s1",
                tmp_path / "transcript",
                "/",
                None,
                "http://127.0.0.1",
            )
        )
        with pytest.raises(RuntimeError, match="checked credential"):
            await stream
    assert received == ["Bearer first-access", "Bearer second-access"]
