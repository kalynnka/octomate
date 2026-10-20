"""Owned artifact views re-read resources without replaying tool execution."""

from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.apps import UI_MIME_TYPE
from fastmcp.resources import ResourceContent, ResourceResult
from mcp.types import BlobResourceContents, TextResourceContents
from pydantic import AnyHttpUrl
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.auth import current_user
from octomate.database import async_session
from octomate.managers.artifacts import (
    ArtifactSandboxUnavailable,
    ArtifactUnavailable,
    ArtifactViewerUnavailable,
)
from octomate.managers.auth import AuthManager
from octomate.schemas.artifacts import InstalledMcpAppSource, McpAppArtifact, McpAppView
from octomate.schemas.mcp import NoAuthMcp, OAuthMcp
from octomate.schemas.oauth import OAuthConnection
from octomate.schemas.thread import Thread, ThreadArtifact, ThreadLedger, ThreadMessage
from octomate.schemas.user import User
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.agent.test_mcp_app_ledger import app_entry as app_entry
from tests.managers.test_mcp import connected
from tests.support.managers import a_thread
from tests.support.users import a_user, auth_config


@dataclass
class SavedApp:
    host: Octomate
    user: User
    artifact: McpAppArtifact
    entry: ThreadArtifact
    upstream: FastMCP
    content: ResourceContent

    @property
    def source(self) -> InstalledMcpAppSource:
        assert isinstance(self.artifact.source, InstalledMcpAppSource)
        return self.artifact.source

    @property
    def url(self) -> str:
        return f"/api/artifacts/{self.artifact.id}"

    @property
    def thread_url(self) -> str:
        return (
            f"/api/trunkline/threads/{self.entry.thread_id}/artifacts/{self.entry.id}"
        )


@pytest.fixture
async def saved_app(
    app_artifact: McpAppArtifact, app_entry: ThreadArtifact
) -> SavedApp:
    host = Octomate()
    host.config.artifacts.sandbox_origin = AnyHttpUrl("https://sandbox.example")
    async with async_session() as session:
        session.add(app_entry)
        await session.commit()
        user = await session.get(User, app_artifact.owner_id)
    assert user is not None
    host.dependency_overrides[current_user] = lambda: user
    upstream = FastMCP("diagrams")
    content = ResourceContent(
        "<!doctype html><html><body>diagram ✓</body></html>",
        mime_type=UI_MIME_TYPE,
        meta={
            "ui": {
                "csp": {
                    "connectDomains": ["https://api.example", "wss://events.example"],
                    "resourceDomains": ["https://*.cdn.example"],
                    "frameDomains": ["https://viewer.example"],
                    "baseUriDomains": ["https://cdn.example"],
                },
                "prefersBorder": True,
                "permissions": {"camera": {}},
                "vendorHint": "preserved",
            }
        },
    )

    @upstream.resource(str(app_artifact.invocation.resource_uri))
    def ui() -> ResourceResult:
        return ResourceResult([content])

    return SavedApp(host, user, app_artifact, app_entry, upstream, content)


@pytest.mark.parametrize("uninstalled", [False, True])
@pytest.mark.parametrize(
    ("origin", "path"),
    [
        ("https://console.example", "/artifacts"),
        ("http://localhost:5173/", "/ui/artifacts"),
        ("http://[::1]:5173", "/generated"),
    ],
)
async def test_overview_uses_saved_metadata_without_upstream_requests(
    saved_app: SavedApp, origin: str, path: str, uninstalled: bool
) -> None:
    case = saved_app
    case.host.config.artifacts.viewer_origin = AnyHttpUrl(origin)
    case.host.config.artifacts.path = path
    case.host.config.artifacts.sandbox_origin = None
    if uninstalled:
        await case.host.mcp.uninstall(case.user.id, case.source.installation_id)
    async with connected(case.host.mcp, case.upstream) as requests:
        overview = await case.host.artifacts.overview(case.artifact.id, case.user)
        assert overview.model_dump(mode="json") == {
            "artifact_id": str(case.artifact.id),
            "title": case.artifact.title,
            "provider_name": case.artifact.invocation.provider_name,
            "viewer_url": f"{origin.rstrip('/')}{path}/{case.artifact.id}",
        }
        assert requests.methods == []


@pytest.mark.parametrize("missing", [False, True])
async def test_overview_requires_artifact_ownership(
    saved_app: SavedApp, missing: bool
) -> None:
    case = saved_app
    case.host.config.artifacts.viewer_origin = AnyHttpUrl("https://console.example")
    user = case.user if missing else await a_user("bob")
    artifact_id = uuid7() if missing else case.artifact.id
    async with connected(case.host.mcp, case.upstream) as requests:
        with pytest.raises(ArtifactUnavailable, match="Artifact unavailable"):
            await case.host.artifacts.overview(artifact_id, user)
        assert requests.methods == []


async def test_overview_requires_a_viewer_origin(saved_app: SavedApp) -> None:
    case = saved_app
    async with connected(case.host.mcp, case.upstream) as requests:
        with pytest.raises(
            ArtifactViewerUnavailable, match=r"artifacts\.viewer_origin"
        ):
            await case.host.artifacts.overview(case.artifact.id, case.user)
        assert requests.methods == []


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("thread", [False, True])
async def test_view_reloads_html_and_preserves_invocation(
    saved_app: SavedApp, binary: bool, thread: bool
) -> None:
    case = saved_app
    async with (
        connected(case.host.mcp, case.upstream) as requests,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        for version in (1, 2):
            html = (
                f"<!doctype html><html><body>diagram ✓ version {version}</body></html>"
            )
            case.content.content = html.encode() if binary else html
            response = await client.get(
                case.thread_url if thread else case.url,
                params={"resource_uri": "ui://other/never-read.html"},
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            # Optional MCP fields use omission on the wire; SDK schemas reject null.
            assert (
                "description" not in response.json()["artifact"]["invocation"]["tool"]
            )
            view = McpAppView.model_validate(response.json())
            assert view.artifact.invocation == case.artifact.invocation
            assert view.resource.html == html
            assert view.resource.uri == case.artifact.invocation.resource_uri
            assert view.resource.ui.prefers_border is True
            assert view.resource.ui.model_extra == {"vendorHint": "preserved"}
            assert view.resource.ui.csp is not None
            assert view.resource.ui.csp.resource_domains == ["https://*.cdn.example"]
            assert str(view.sandbox_origin) == "https://sandbox.example/"
        assert requests.methods.count("resources/read") == 2
        assert "tools/list" not in requests.methods
        assert "tools/call" not in requests.methods
        assert "resources/list" not in requests.methods


@pytest.mark.parametrize(
    "state",
    [
        "anonymous",
        "other_user",
        "thread_participant",
        "missing",
        "disabled",
        "removed",
        "reinstalled",
        "foreign_installation",
    ],
)
async def test_unavailable_artifacts_do_not_fetch_resources(
    saved_app: SavedApp, state: str
) -> None:
    case = saved_app
    url = case.url
    expected = 404
    if state == "anonymous":
        case.host.auth = AuthManager(auth_config())
        case.host.dependency_overrides.clear()
        expected = 401
    elif state in {"other_user", "thread_participant"}:
        other = await a_user("bob", profiles={"im": "bob"})
        case.host.dependency_overrides[current_user] = lambda: other
        if state == "thread_participant":
            profile = await case.host.users.profile("im", "bob")
            assert profile is not None
            async with async_session() as session:
                session.add(
                    ThreadMessage(
                        thread_id=case.entry.thread_id,
                        direction="inbound",
                        actor_kind="human",
                        sender_id=profile.id,
                    )
                )
                await session.commit()
            url = case.thread_url
    elif state == "missing":
        url = f"/api/artifacts/{uuid7()}"
    elif state == "disabled":
        await case.host.mcp.disable(
            user_id=case.user.id, mcp_id=case.source.installation_id
        )
    elif state in {"removed", "reinstalled"}:
        await case.host.mcp.uninstall(case.user.id, case.source.installation_id)
        if state == "reinstalled":
            async with async_session() as session:
                session.add(
                    NoAuthMcp(
                        user_id=case.user.id,
                        name="Replacement",
                        namespace=case.source.namespace,
                        url="https://replacement.example/mcp",
                    )
                )
                await session.commit()
    else:
        other = await a_user("bob")
        async with async_session() as session:
            instance = NoAuthMcp(
                user_id=other.id,
                name="Other diagrams",
                namespace=case.source.namespace,
                url="https://mcp.example/mcp",
            )
            session.add(instance)
            artifact = await session.get(McpAppArtifact, case.artifact.id)
            assert artifact is not None
            artifact.source = case.source.model_copy(
                update={"installation_id": instance.id}
            )
            await session.commit()
    async with (
        connected(case.host.mcp, case.upstream) as requests,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        response = await client.get(url)
        assert response.status_code == expected
        if state != "anonymous":
            assert response.headers["cache-control"] == "no-store"
        assert "invocation" not in response.text
        assert requests.methods == []


@pytest.mark.parametrize(
    "origin", [None, "https://console.example", "https://console.example:443/"]
)
async def test_view_requires_separate_sandbox_origin(
    saved_app: SavedApp, origin: str | None
) -> None:
    case = saved_app
    case.host.config.artifacts.sandbox_origin = AnyHttpUrl(origin) if origin else None
    async with (
        connected(case.host.mcp, case.upstream) as requests,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        response = await client.get(case.url)
        # The configured sandbox host now exposes no API routes at all.
        assert response.status_code == (404 if origin else 503)
        with pytest.raises(ArtifactSandboxUnavailable, match="separate"):
            await case.host.artifacts.load(
                case.artifact.id,
                case.user,
                console_origin=AnyHttpUrl("https://console.example"),
            )
        assert requests.methods == []


async def test_thread_access_membership_and_fork_cutoff(saved_app: SavedApp) -> None:
    case = saved_app
    fork = await a_thread("fork")
    inaccessible = await a_thread("inaccessible")
    unrelated = await a_thread("unrelated")
    async with async_session() as session:
        session.add(
            ThreadMessage(
                thread_id=fork,
                direction="inbound",
                actor_kind="human",
                sender_id=case.entry.sender_id,
            )
        )
        session.add(
            ThreadLedger(
                thread_id=fork,
                ledger_id=case.entry.thread_id,
                cut_message_id=case.entry.id,
            )
        )
        later = ThreadArtifact(
            thread_id=case.entry.thread_id,
            direction="outbound",
            actor_kind="agent",
            sender_id=case.entry.sender_id,
            artifact_id=case.artifact.id,
        )
        session.add(later)
        session.add(
            ThreadMessage(
                thread_id=unrelated,
                direction="inbound",
                actor_kind="human",
                sender_id=case.entry.sender_id,
            )
        )
        await session.commit()
    async with (
        connected(case.host.mcp, case.upstream) as requests,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        response = await client.get(
            f"/api/trunkline/threads/{fork}/artifacts/{case.entry.id}"
        )
        assert response.status_code == 200
        for thread_id, message_id in (
            (fork, later.id),
            (fork, uuid7()),
            (inaccessible, case.entry.id),
            (unrelated, case.entry.id),
            (uuid7(), case.entry.id),
        ):
            response = await client.get(
                f"/api/trunkline/threads/{thread_id}/artifacts/{message_id}"
            )
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
        assert requests.methods.count("resources/read") == 1
        assert "tools/call" not in requests.methods


async def test_owned_artifact_remains_loadable_after_thread_deletion(
    saved_app: SavedApp,
) -> None:
    case = saved_app
    async with async_session() as session:
        thread = await session.get(Thread, case.entry.thread_id)
        assert thread is not None
        await session.delete(thread)
        await session.commit()
    async with (
        connected(case.host.mcp, case.upstream),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        assert (await client.get(case.url)).status_code == 200
        assert (await client.get(case.thread_url)).status_code == 404


@pytest.mark.parametrize("grant", [False, True])
async def test_missing_or_revoked_oauth_grant_blocks_loading(
    saved_app: SavedApp, grant: bool
) -> None:
    case = saved_app
    await case.host.mcp.uninstall(case.user.id, case.source.installation_id)
    async with async_session() as session:
        instance = OAuthMcp(
            user_id=case.user.id,
            name="OAuth diagrams",
            namespace=case.source.namespace,
            url="https://mcp.example/mcp",
        )
        session.add(instance)
        await session.flush()
        artifact = await session.get(McpAppArtifact, case.artifact.id)
        assert artifact is not None
        artifact.source = case.source.model_copy(
            update={"installation_id": instance.id}
        )
        if grant:
            session.add(
                OAuthConnection(
                    user_id=case.user.id,
                    mcp_id=instance.id,
                    connector_id="mcp",
                    status="invalid",
                    encrypted_tokens=b"revoked provider secret",
                )
            )
        await session.commit()
    async with (
        connected(case.host.mcp, case.upstream) as requests,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        response = await client.get(case.url)
        assert response.status_code == 404
        assert "secret" not in response.text
        assert requests.methods == []


@pytest.mark.parametrize(
    "invalid",
    [
        "uri",
        "mime",
        "empty",
        "whitespace",
        "surrogate",
        "missing",
        "multiple",
        "base64",
        "utf8",
        "metadata",
        "csp",
        "upstream",
    ],
)
async def test_invalid_resources_return_safe_errors(
    saved_app: SavedApp, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    case = saved_app
    text = TextResourceContents(
        uri=str(case.artifact.invocation.resource_uri),
        mime_type=UI_MIME_TYPE,
        text="<!doctype html><html></html>",
    )
    contents: list[TextResourceContents | BlobResourceContents] = [text]
    if invalid == "uri":
        text.uri = "ui://other/app.html"
    elif invalid == "mime":
        text.mime_type = "text/html"
    elif invalid == "empty":
        text.text = ""
    elif invalid == "whitespace":
        text.text = " \n "
    elif invalid == "surrogate":
        text.text = "\ud800"
    elif invalid == "missing":
        contents = []
    elif invalid == "multiple":
        contents.append(text)
    elif invalid in {"base64", "utf8"}:
        contents = [
            BlobResourceContents(
                uri=text.uri,
                mime_type=UI_MIME_TYPE,
                blob="not base64!" if invalid == "base64" else "/w==",
            )
        ]
    elif invalid == "metadata":
        text.meta = {"ui": []}
    elif invalid == "csp":
        text.meta = {
            "ui": {"csp": {"resourceDomains": ["https://cdn.example; script-src *"]}}
        }
    read = AsyncMock(return_value=contents)
    if invalid == "upstream":
        read.side_effect = TimeoutError("provider credential must stay private")
    monkeypatch.setattr(Client, "read_resource", read)
    async with (
        connected(case.host.mcp, case.upstream) as requests,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        response = await client.get(case.url)
        assert response.status_code == 502
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"detail": "MCP app resource could not be loaded"}
        read.assert_awaited_once_with(case.artifact.invocation.resource_uri)
        assert "tools/call" not in requests.methods


@pytest.mark.parametrize(
    "domain",
    [
        "*",
        "'self'",
        "https://user:secret@cdn.example",
        "https://cdn.example/path",
        "https://cdn.example?token=secret",
        "https://cdn.example#fragment",
        "https://cdn.example\n",
        "https://*",
        "data:",
        "javascript:alert(1)",
    ],
)
async def test_resource_policy_rejects_non_origin_sources(
    saved_app: SavedApp, domain: str
) -> None:
    case = saved_app
    case.content.meta = {"ui": {"csp": {"resourceDomains": [domain]}}}
    async with (
        connected(case.host.mcp, case.upstream),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="https://console.example",
        ) as client,
    ):
        assert (await client.get(case.url)).status_code == 502


async def test_local_separate_port_and_absent_ui_metadata(saved_app: SavedApp) -> None:
    case = saved_app
    case.content.meta = None
    case.host.config.artifacts.sandbox_origin = AnyHttpUrl("http://localhost:8001")
    async with (
        connected(case.host.mcp, case.upstream),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=case.host),
            base_url="http://localhost:8000",
        ) as client,
    ):
        response = await client.get(case.url)
        assert response.status_code == 200
        view = McpAppView.model_validate(response.json())
        assert view.resource.ui.csp is None
        assert view.resource.ui.permissions is None
        assert str(view.sandbox_origin) == "http://localhost:8001/"
