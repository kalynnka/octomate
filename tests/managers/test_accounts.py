import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import AuthConfig, OctomateConfig
from octomate.config.channels import AgentModelConfig, TrunklineChannelConfig
from octomate.database import AsyncSession, async_session
from octomate.managers.auth import AuthManager, InvalidCredentials, UsernameUnavailable
from octomate.managers.user import UserManager
from octomate.schemas.auth import UserInvitation, UserSession
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.trunkline import TrunklineTentacle
from tests.support.agents import build_scripted_agent

PASSWORD = "Correct horse battery staple1!"


@pytest.fixture
async def app(in_memory_engine: AsyncEngine) -> Octomate:
    application = Octomate(
        config=OctomateConfig(
            auth=AuthConfig(
                access_token_salt=SecretStr("access-token-test-salt"),
                refresh_token_salt=SecretStr("refresh-token-test-salt"),
                api_key_salt=SecretStr("api-key-test-salt"),
            )
        )
    )
    agent, _ = build_scripted_agent(["done", "done again"])
    assert agent.model is not None
    application.connect(
        InklingTentacle(
            "inkling", application, agent=agent, models={"test": agent.model}
        )
    )
    channel = application.connect(
        TrunklineTentacle(
            "trunkline",
            application,
            config=TrunklineChannelConfig(
                agents=[AgentModelConfig(agent="inkling", model="test")]
            ),
        )
    )
    await channel.probe()
    return application


@pytest.fixture
def auth(app: Octomate) -> AuthManager:
    assert app.auth is not None
    return app.auth


@pytest.fixture
async def client(app: Octomate) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"X-Octomate-Request": "1"},
    ) as connected:
        yield connected


async def enroll(
    client: httpx.AsyncClient, auth: AuthManager, username: str = "alice"
) -> httpx.Response:
    invitation = await auth.invite()
    return await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            "name": username.title(),
            "invitation": invitation.get_secret_value(),
        },
    )


async def test_registration_password_schema_describes_requirements(
    client: httpx.AsyncClient,
) -> None:
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    password = schemas["RegistrationBody"]["properties"]["password"]
    assert password["minLength"] == 11
    assert password["maxLength"] == 1024
    assert password["writeOnly"] is True
    for requirement in ("lowercase", "uppercase", "digit", "symbol"):
        assert requirement in password["description"]
    assert schemas["LoginBody"]["properties"]["password"]["minLength"] == 1


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("Abcdefgh1!", id="ten-characters"),
        pytest.param("Aa1!" + "a" * 1021, id="too-long"),
        pytest.param("ABCDEFGHI1!", id="no-lowercase"),
        pytest.param("abcdefghi1!", id="no-uppercase"),
        pytest.param("Abcdefghij!", id="no-digit"),
        pytest.param("Abcdefghij1", id="no-symbol"),
        pytest.param("Abcdefghi1 ", id="whitespace-is-not-a-symbol"),
    ],
)
async def test_registration_rejects_weak_passwords_without_consuming_invitation(
    client: httpx.AsyncClient, auth: AuthManager, password: str
) -> None:
    token = await auth.invite()
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "password": password,
            "name": "Alice",
            "invitation": token.get_secret_value(),
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "password"]
    assert password not in response.text
    assert token.get_secret_value() not in response.text
    async with async_session() as session:
        [invitation] = await session.list(UserInvitation)
        assert invitation.consumed_at is None
        assert await session.list(User) == []
        assert await session.list(UserSession) == []


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("Abcdefghi1!", id="eleven-characters"),
        pytest.param("Aa1!" + "a" * 1020, id="maximum-length"),
        pytest.param("Abcdefghi1_", id="underscore-is-a-symbol"),
    ],
)
async def test_registration_accepts_passwords_meeting_requirements(
    client: httpx.AsyncClient, auth: AuthManager, password: str
) -> None:
    token = await auth.invite()
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "password": password,
            "name": "Alice",
            "invitation": token.get_secret_value(),
        },
    )
    assert response.status_code == 200, response.text
    assert (await client.get("/api/auth/me")).json()["username"] == "alice"


async def test_account_response_schemas_match_the_returned_fields(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    document = (await client.get("/openapi.json")).json()
    assert "UserInfo" not in document["components"]["schemas"]
    registered = await enroll(client, auth)
    account = await client.get("/api/auth/me")
    for method, path, response in (
        ("post", "/api/auth/register", registered),
        ("get", "/api/auth/me", account),
    ):
        assert response.status_code == 200
        reference = document["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert reference == "#/components/schemas/User"
        schema = document["components"]["schemas"][reference.rsplit("/", 1)[1]]
        assert (
            set(schema["properties"])
            == set(response.json())
            == {"id", "username", "name", "nickname"}
        )
        assert set(schema["required"]) == set(response.json())


async def test_api_key_response_schema_describes_the_disclosed_token(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    operations = document["paths"]["/api/auth/api-keys"]
    reference = operations["post"]["responses"]["201"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    issued_schema = schemas[reference.rsplit("/", 1)[1]]
    token_schema = issued_schema["properties"]["token"]
    assert token_schema["type"] == "string"
    assert not token_schema.get("writeOnly", False)

    await enroll(client, auth)
    issued = await client.post(
        "/api/auth/api-keys", json={"name": "Laptop", "scopes": ["hooks", "mcp"]}
    )
    assert issued.status_code == 201
    assert set(issued_schema["properties"]) == set(issued.json())
    key_reference = issued_schema["properties"]["key"]["$ref"]
    key_schema = schemas[key_reference.rsplit("/", 1)[1]]
    assert set(key_schema["properties"]) == set(issued.json()["key"])
    assert "key_hash" not in key_schema["properties"]
    listed_schema = operations["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert listed_schema["type"] == "array"
    assert listed_schema["items"]["$ref"] == key_reference
    assert (await client.get("/api/auth/api-keys")).json() == [issued.json()["key"]]


async def test_registration_login_rotation_and_logout(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401
    registered = await enroll(client, auth)
    assert registered.status_code == 200, registered.text
    assert set(registered.json()) == {"id", "username", "name", "nickname"}
    for cookie in registered.headers.get_list("set-cookie"):
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/api" in cookie
    assert registered.headers["cache-control"] == "no-store"
    account = (await client.get("/api/auth/me")).json()
    assert account["name"] == "Alice"
    old_access = client.cookies["octomate_access"]
    old_refresh = client.cookies["octomate_refresh"]
    assert (await client.post("/api/auth/refresh")).status_code == 204
    assert client.cookies["octomate_access"] != old_access
    assert client.cookies["octomate_refresh"] != old_refresh
    assert await auth.authenticate_session(SecretStr(old_access)) is None
    with pytest.raises(InvalidCredentials):
        await auth.refresh_session(SecretStr(old_refresh))
    refresh = client.cookies["octomate_refresh"]
    assert (await client.post("/api/auth/logout")).status_code == 204
    assert not client.cookies
    assert (await client.get("/api/auth/me")).status_code == 401
    with pytest.raises(InvalidCredentials):
        await auth.refresh_session(SecretStr(refresh))
    assert (
        await client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
    ).status_code == 204
    assert (await client.get("/api/auth/me")).json()["id"] == account["id"]


async def test_registered_identity_and_bindings_survive_restart(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    result = await enroll(client, auth)
    assert result.status_code == 200
    user_id = uuid.UUID(result.json()["id"])
    async with async_session() as session:
        session.add(
            UserProfile(
                channel_tentacle_id="slack", channel_user_id="U1", user_id=user_id
            )
        )
        await session.commit()
    manager = UserManager()
    profile = await manager.profile("slack", "U1")
    assert profile is not None
    assert profile.user_id == user_id
    owner = await manager.owner(profile)
    assert owner is not None
    assert owner.name == "Alice"
    assert (await auth.login("alice", SecretStr(PASSWORD))).user_id == user_id


async def test_anonymous_invitations_are_independent_and_single_use(
    auth: AuthManager,
) -> None:
    first = await auth.invite()
    second = await auth.invite()
    async with async_session() as session:
        assert await session.list(User) == []
        assert len(await session.list(UserInvitation)) == 2
    with pytest.raises(InvalidCredentials):
        await auth.register(
            "alice", SecretStr(PASSWORD), SecretStr("unknown"), name="Alice"
        )
    alice = await auth.register("alice", SecretStr(PASSWORD), first, name="Alice")
    bob = await auth.register("bob", SecretStr(PASSWORD), second, name="Bob")
    assert alice.id != bob.id
    for token in (first, second):
        with pytest.raises(InvalidCredentials):
            await auth.register("charlie", SecretStr(PASSWORD), token, name="Charlie")


@pytest.mark.parametrize("has_password", [False, True])
async def test_invitation_cannot_claim_an_existing_username(
    client: httpx.AsyncClient, auth: AuthManager, has_password: bool
) -> None:
    password_hash = (
        await auth.hash_password(SecretStr(PASSWORD)) if has_password else None
    )
    user = User(username="alice", name="Original", password_hash=password_hash)
    async with async_session() as session:
        session.add(user)
        await session.flush()
        profile = UserProfile(
            channel_tentacle_id="slack", channel_user_id="U1", user_id=user.id
        )
        session.add(profile)
        await session.commit()
    invitation = await auth.invite()
    payload = {
        "username": "alice",
        "password": "An intruder's different password1!",
        "name": "Intruder",
        "invitation": invitation.get_secret_value(),
    }
    assert (await client.post("/api/auth/register", json=payload)).status_code == 409
    assert not client.cookies
    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        assert stored.name == "Original"
        assert stored.password_hash == password_hash
        stored_profile = await session.get(UserProfile, profile.id)
        assert stored_profile is not None
        assert stored_profile.user_id == user.id
        [code] = await session.list(UserInvitation)
        assert code.consumed_at is None
    payload["username"] = "bob"
    result = await client.post("/api/auth/register", json=payload)
    assert result.status_code == 200
    assert result.json()["id"] != str(user.id)


async def test_expired_invitation_cannot_enroll(auth: AuthManager) -> None:
    invitation = await auth.invite()
    async with async_session() as session:
        [stored] = await session.list(UserInvitation)
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(InvalidCredentials):
        await auth.register("alice", SecretStr(PASSWORD), invitation, name="Alice")
    async with async_session() as session:
        assert await session.list(User) == []


async def test_concurrent_enrollment_consumes_an_invitation_once(
    auth: AuthManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation = await auth.invite()
    barrier = asyncio.Barrier(2)
    flush = AsyncSession.flush

    async def simultaneous_flush(session: AsyncSession) -> None:
        await barrier.wait()
        await flush(session)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", simultaneous_flush)
        results = await asyncio.wait_for(
            asyncio.gather(
                auth.register("alice", SecretStr(PASSWORD), invitation, name="Alice"),
                AuthManager(auth.config).register(
                    "bob", SecretStr(PASSWORD), invitation, name="Bob"
                ),
                return_exceptions=True,
            ),
            timeout=10,
        )
    assert sum(isinstance(result, User) for result in results) == 1
    assert sum(isinstance(result, InvalidCredentials) for result in results) == 1
    async with async_session() as session:
        [user] = await session.list(User)
        [code] = await session.list(UserInvitation)
        assert code.consumed_at is not None
    assert (await auth.login(user.username, SecretStr(PASSWORD))).user_id == user.id


async def test_concurrent_registrations_cannot_take_the_same_username(
    auth: AuthManager,
) -> None:
    invitations = [await auth.invite(), await auth.invite()]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                auth.register("alice", SecretStr(PASSWORD), invitation, name="Alice")
                for invitation in invitations
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert sum(isinstance(result, User) for result in results) == 1
    assert sum(isinstance(result, UsernameUnavailable) for result in results) == 1
    async with async_session() as session:
        assert len(await session.list(User)) == 1
        codes = await session.list(UserInvitation)
        assert sum(code.consumed_at is not None for code in codes) == 1
    for invitation, result in zip(invitations, results, strict=True):
        if isinstance(result, UsernameUnavailable):
            await auth.register("bob", SecretStr(PASSWORD), invitation, name="Bob")


@pytest.mark.parametrize("operation", ["flush", "commit"])
async def test_registration_does_not_mask_unexpected_database_errors(
    auth: AuthManager, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    invitation = await auth.invite()
    error = IntegrityError(None, None, ValueError("unexpected constraint failure"))

    async def fail(session: AsyncSession) -> None:
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, operation, fail)
        with pytest.raises(IntegrityError) as caught:
            await auth.register("alice", SecretStr(PASSWORD), invitation, name="Alice")
        assert caught.value is error
    async with async_session() as session:
        assert await session.list(User) == []
        [code] = await session.list(UserInvitation)
        assert code.consumed_at is None
    await auth.register("alice", SecretStr(PASSWORD), invitation, name="Alice")


async def test_browser_writes_require_csrf_header_and_validation_hides_secrets(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    token = await auth.invite()
    payload = {
        "username": "alice",
        "password": "too-short",
        "name": "Alice",
        "invitation": token.get_secret_value(),
    }
    client.headers.pop("X-Octomate-Request")
    refused = await client.post("/api/auth/register", json=payload)
    assert refused.status_code == 403
    rejected = await client.post(
        "/api/auth/register", json=payload, headers={"X-Octomate-Request": "1"}
    )
    assert rejected.status_code == 422
    assert "too-short" not in rejected.text
    assert token.get_secret_value() not in rejected.text
    payload["password"] = PASSWORD
    accepted = await client.post(
        "/api/auth/register", json=payload, headers={"X-Octomate-Request": "1"}
    )
    assert accepted.status_code == 200
    assert (
        await client.post("/api/trunkline/threads/a/messages", json={"text": "hello"})
    ).status_code == 403


async def test_expired_access_can_refresh_but_revoked_session_cannot(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    await enroll(client, auth)
    async with async_session() as session:
        [current] = await session.list(UserSession)
        current.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.post("/api/auth/refresh")).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_private_threads_and_mutations_are_isolated(
    app: Octomate, auth: AuthManager, client: httpx.AsyncClient
) -> None:
    assert (await client.get("/api/trunkline/threads")).status_code == 401
    alice = (await enroll(client, auth)).json()
    streamed = await client.post(
        "/api/trunkline/threads/shared-key/messages",
        json={"text": "Alice's private message", "model": "inkling:test"},
    )
    assert streamed.status_code == 200
    assert '"event_kind":"run_error"' not in streamed.text
    [thread] = (await client.get("/api/trunkline/threads")).json()
    assert thread["chat_id"] == alice["id"]
    assert "parent" not in thread
    path = f"/api/trunkline/threads/{thread['id']}"
    [conversation] = (await client.get(f"{path}/conversations")).json()
    stored_conversation = await app.conversations.get(uuid.UUID(conversation["id"]))
    address = ChannelAddress(
        channel_tentacle_id="trunkline",
        chat_type="thread",
        chat_id=alice["id"],
        user_id=alice["id"],
        channel_thread_id="shared-key",
    )
    batch = await app.deferred_actions.create_batch(
        conversation=stored_conversation,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=None,
        requests=DeferredToolRequests(
            approvals=[
                ToolCallPart(tool_name="sensitive", args={}, tool_call_id="approval-1")
            ]
        ),
    )
    await enroll(client, auth, "bob")
    assert (await client.get("/api/trunkline/threads")).json() == []
    for suffix in ("", "/messages", "/conversations", "/project", "/batches"):
        response = await client.get(path + suffix)
        assert response.status_code == 404, (suffix, response.text)
    assert (
        await client.patch(
            f"/api/trunkline/conversations/{conversation['id']}/permission-mode",
            json={"permission_mode": "dontAsk"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/trunkline/batches/{batch.id}/resolve",
            json={"approvals": {str(next(iter(batch.approvals)).id): True}},
        )
    ).status_code == 404
    assert (await app.deferred_actions.get_batch(batch.id)).status == "pending"
    second = await client.post(
        "/api/trunkline/threads/shared-key/messages",
        json={
            "text": "Bob's private message",
            "model": "inkling:test",
            "user_id": alice["id"],
        },
    )
    assert second.status_code == 200
    [bob_thread] = (await client.get("/api/trunkline/threads")).json()
    assert bob_thread["id"] != thread["id"]
    assert bob_thread["chat_id"] != thread["chat_id"]
    messages = (
        await client.get(f"/api/trunkline/threads/{bob_thread['id']}/messages")
    ).json()
    assert messages[0]["sender"]["user_id"] == bob_thread["chat_id"]
    assert "Alice's private message" not in str(messages)


async def test_api_keys_are_issued_once_and_managed_only_by_their_owner(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    assert (await client.get("/api/auth/api-keys")).status_code == 401
    await enroll(client, auth)
    issued = await client.post(
        "/api/auth/api-keys", json={"name": "Laptop", "scopes": ["hooks", "mcp"]}
    )
    assert issued.status_code == 201
    assert issued.headers["Cache-Control"] == "no-store"
    token = issued.json()["token"]
    key_id = issued.json()["key"]["id"]
    assert token.startswith(auth.config.api_key_prefix)
    assert "key_hash" not in issued.json()["key"]
    assert await auth.authenticate_api_key(SecretStr(token), scope="hooks") is not None
    assert await auth.authenticate_api_key(SecretStr(token), scope="mcp") is not None
    listed = await client.get("/api/auth/api-keys")
    assert [key["id"] for key in listed.json()] == [key_id]
    assert token not in listed.text
    assert "key_hash" not in listed.text

    await enroll(client, auth, "bob")
    assert (await client.get("/api/auth/api-keys")).json() == []
    assert (await client.delete(f"/api/auth/api-keys/{key_id}")).status_code == 404
    assert await auth.authenticate_api_key(SecretStr(token), scope="mcp") is not None

    await client.post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert (await client.delete(f"/api/auth/api-keys/{key_id}")).status_code == 204
    assert await auth.authenticate_api_key(SecretStr(token), scope="hooks") is None
    assert await auth.authenticate_api_key(SecretStr(token), scope="mcp") is None


async def test_api_key_creation_rejects_invalid_scope_expiry_and_cross_origin_writes(
    client: httpx.AsyncClient, auth: AuthManager
) -> None:
    await enroll(client, auth)
    for body in (
        {"name": "Laptop", "scopes": []},
        {"name": "Laptop", "scopes": ["admin"]},
        {"name": "Laptop", "scopes": ["hooks"], "expires_at": "2000-01-01T00:00:00Z"},
        {"name": "Laptop", "scopes": ["hooks"], "expires_at": "2099-01-01T00:00:00"},
    ):
        assert (await client.post("/api/auth/api-keys", json=body)).status_code == 422
    client.headers.pop("X-Octomate-Request")
    denied = await client.post(
        "/api/auth/api-keys", json={"name": "Laptop", "scopes": ["hooks"]}
    )
    assert denied.status_code == 403
    assert (await client.get("/api/auth/api-keys")).json() == []
