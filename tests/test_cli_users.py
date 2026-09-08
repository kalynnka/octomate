import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from octomate_cli import users as user_cli
from octomate_cli.config import project_config_path, user_config_path
from octomate_cli.main import app as cli
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import async_session
from octomate.managers.auth import AuthManager
from octomate.schemas.auth import UserApiKey, UserInvitation, UserSession
from octomate.schemas.user import User
from tests.support.users import auth_config

PASSWORD = "Correct horse battery staple1!"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "config"
    config.mkdir()
    config.joinpath("auth.yaml").write_text(
        "auth:\n"
        "  access_token_salt: access-token-test-salt\n"
        "  refresh_token_salt: refresh-token-test-salt\n"
        "  api_key_salt: api-key-test-salt\n"
    )
    monkeypatch.setenv("OCTOMATE_HOME", str(config))


@pytest.fixture
async def auth(in_memory_engine: AsyncEngine) -> AuthManager:
    return AuthManager(auth_config())


@pytest.mark.parametrize("link", [False, True])
async def test_invite_prints_a_usable_code_or_link(
    auth: AuthManager, link: bool
) -> None:
    args = ["invite"]
    if link:
        args += ["--url", "https://octomate.example.com"]
    result = await asyncio.to_thread(runner.invoke, cli, args)
    assert result.exit_code == 0, result.output
    output = result.stdout.strip()
    token = parse_qs(urlsplit(output).fragment)["invitation"][0] if link else output
    async with async_session() as session:
        [invitation] = await session.list(UserInvitation)
        assert invitation.consumed_at is None
        assert invitation.token_hash.get_secret_value() != token
        assert await session.list(User) == []
    user = await auth.register(
        "alice", SecretStr(PASSWORD), SecretStr(token), name="Alice"
    )
    assert user.username == "alice"


@pytest.mark.parametrize("name", [None, "Alice"])
async def test_create_consumes_the_code_and_can_sign_in_through_the_ui(
    auth: AuthManager, name: str | None
) -> None:
    code = (await auth.invite()).get_secret_value()
    args = [
        "user",
        "create",
        "--username",
        "alice",
        "--password",
        PASSWORD,
        "--invitationcode",
        code,
    ]
    if name is not None:
        args += ["--name", name]
    result = await asyncio.to_thread(runner.invoke, cli, args)
    assert result.exit_code == 0, result.output
    assert "Sign in through Trunkline" in result.output
    assert PASSWORD not in result.output
    assert code not in result.output
    assert not user_config_path().exists()
    assert not project_config_path().exists()
    async with async_session() as session:
        [user] = await session.list(User)
        [invitation] = await session.list(UserInvitation)
        assert user.name == (name if name is not None else "alice")
        assert user.password_hash is not None
        assert user.password_hash.get_secret_value().startswith("$argon2id$")
        assert invitation.consumed_at is not None
        assert await session.list(UserSession) == []
        assert await session.list(UserApiKey) == []
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"X-Octomate-Request": "1"},
    ) as client:
        response = await client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 204
        assert (await client.get("/api/auth/me")).json()["id"] == str(user.id)


@pytest.mark.parametrize("code_state", ["unknown", "used", "expired"])
async def test_create_rejects_invalid_codes(auth: AuthManager, code_state: str) -> None:
    code = await auth.invite()
    if code_state == "used":
        await auth.register("owner", SecretStr(PASSWORD), code, name="Owner")
    elif code_state == "expired":
        async with async_session() as session:
            [invitation] = await session.list(UserInvitation)
            invitation.expires_at = invitation.expires_at.replace(year=2000)
            await session.commit()
    else:
        code = SecretStr("unknown-invitation")
    result = await asyncio.to_thread(
        runner.invoke,
        cli,
        [
            "user",
            "create",
            "--username",
            "alice",
            "--password",
            PASSWORD,
            "--invitationcode",
            code.get_secret_value(),
        ],
    )
    assert result.exit_code != 0
    assert "Invalid or expired credentials" in result.output
    assert PASSWORD not in result.output
    assert code.get_secret_value() not in result.output
    async with async_session() as session:
        assert (
            await session.one_or_none(User, expressions=[User["username"] == "alice"])
            is None
        )


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        (" alice", PASSWORD, "Username must"),
        ("alice", "Abcdefg1!", "Use at least 11 characters."),
        ("alice", "Abcdefgh1!", "Use at least 11 characters."),
        ("alice", "Aa1!" + "a" * 1021, "Use at most 1024 characters."),
        ("alice", "no uppercase or digits", "Password must"),
        ("alice", "NoSymbols123", "Password must"),
    ],
)
async def test_invalid_registration_preserves_the_invitation(
    auth: AuthManager, username: str, password: str, message: str
) -> None:
    code = (await auth.invite()).get_secret_value()
    result = await asyncio.to_thread(
        runner.invoke,
        cli,
        [
            "user",
            "create",
            "--username",
            username,
            "--password",
            password,
            "--invitationcode",
            code,
        ],
    )
    assert result.exit_code != 0
    assert message in result.output
    assert password not in result.output
    assert code not in result.output
    assert "function-after" not in result.output
    assert "validation error" not in result.output
    assert "errors.pydantic.dev" not in result.output
    assert "Value error" not in result.output
    if username == "alice":
        assert "--password" in result.output
    async with async_session() as session:
        [invitation] = await session.list(UserInvitation)
        assert invitation.consumed_at is None
        assert await session.list(User) == []


def test_create_requires_an_invitation_code() -> None:
    result = runner.invoke(
        cli, ["user", "create", "--username", "alice", "--password", PASSWORD]
    )
    assert result.exit_code != 0
    assert "--invitationcode" in result.output


def test_create_requires_the_server_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_cli, "find_spec", lambda name: None)
    result = runner.invoke(
        cli,
        [
            "user",
            "create",
            "--username",
            "alice",
            "--password",
            PASSWORD,
            "--invitationcode",
            "unused-code",
        ],
    )
    assert result.exit_code != 0
    assert "server package" in result.output


def test_login_is_not_a_cli_command() -> None:
    assert runner.invoke(cli, ["login"]).exit_code != 0
