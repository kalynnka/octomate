"""Sandbox routing and policy tests; no real database or provider is used."""

import json
from urllib.parse import urlencode

import httpx
import pytest
from pydantic import AnyHttpUrl, ValidationError

from octomate import Octomate
from octomate.artifacts import sandbox_csp
from octomate.schemas.artifacts import ArtifactSandboxPolicy


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/auth/me",
        "/api/artifacts/config",
        "/docs",
        "/openapi.json",
        "/octomate/mcp",
        "/assets/app.js",
    ],
)
async def test_sandbox_origin_exposes_no_console_routes(path: str) -> None:
    app = Octomate()
    app.config.artifacts.sandbox_origin = AnyHttpUrl("https://sandbox.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://sandbox.example"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "origin", ["https://console.example", "http://sandbox.example"]
)
async def test_proxy_is_not_available_on_other_origins(origin: str) -> None:
    app = Octomate()
    app.config.artifacts.sandbox_origin = AnyHttpUrl("https://sandbox.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=origin
    ) as client:
        response = await client.get("/artifacts/sandbox.js")
    assert response.status_code == 404


async def test_sandbox_hostname_cannot_reach_accounts_over_plain_http() -> None:
    app = Octomate()
    app.config.artifacts.sandbox_origin = AnyHttpUrl("https://sandbox.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sandbox.example"
    ) as client:
        response = await client.get("/api/artifacts/config")
    assert response.status_code == 404


async def test_sandbox_returns_policy_headers_and_only_bundled_script() -> None:
    app = Octomate()
    app.config.artifacts.sandbox_origin = AnyHttpUrl("http://localhost:8000")
    policy = ArtifactSandboxPolicy.model_validate(
        {
            "parent_origin": "http://localhost:5173",
            "csp": {
                "connectDomains": ["https://api.example"],
                "resourceDomains": ["https://cdn.example"],
                "frameDomains": ["https://frame.example"],
            },
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        response = await client.get(
            "/artifacts/sandbox?"
            + urlencode({"policy": policy.model_dump_json(by_alias=True)})
        )
        script = await client.get("/artifacts/sandbox.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]
    assert (
        "frame-ancestors http://localhost:5173"
        in response.headers["content-security-policy"]
    )
    assert (
        "connect-src https://api.example" in response.headers["content-security-policy"]
    )
    assert (
        "frame-src 'self' https://frame.example"
        in response.headers["content-security-policy"]
    )
    assert "frame-src https://frame.example" in response.text
    assert script.status_code == 200
    assert "allow-scripts'" in script.text
    assert "allow-same-origin" not in script.text
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "source",
    [
        "https://console.example",
        "http://console.example:8000",
        "wss://console.example",
        "https://*.example",
        "https://*.console.example",
    ],
)
async def test_policy_cannot_allow_console_or_its_subdomain(source: str) -> None:
    parent = (
        "https://child.console.example"
        if source.endswith("*.console.example")
        else "https://console.example"
    )
    with pytest.raises(ValidationError, match="console host"):
        ArtifactSandboxPolicy.model_validate(
            {"parent_origin": parent, "csp": {"connectDomains": [source]}}
        )


@pytest.mark.parametrize(
    "policy",
    [
        "{}",
        "invalid",
        json.dumps({"parent_origin": "https://console.example/path"}),
        json.dumps({"parent_origin": "https://sandbox.example"}),
        json.dumps(
            {
                "parent_origin": "https://console.example",
                "csp": {"resourceDomains": ["https://cdn.example; script-src *"]},
            }
        ),
    ],
)
async def test_proxy_refuses_invalid_policy(policy: str) -> None:
    app = Octomate()
    app.config.artifacts.sandbox_origin = AnyHttpUrl("https://sandbox.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://sandbox.example"
    ) as client:
        response = await client.get("/artifacts/sandbox", params={"policy": policy})
    assert response.status_code == 422


def test_inner_policy_has_no_undeclared_frames_network_or_base_uri() -> None:
    csp = sandbox_csp(
        ArtifactSandboxPolicy(parent_origin=AnyHttpUrl("https://console.example")),
        proxy=False,
    )
    for directive in [
        "connect-src 'none'",
        "frame-src 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "form-action 'none'",
        "worker-src 'none'",
    ]:
        assert directive in csp


async def test_viewer_configuration_uses_the_configured_path() -> None:
    app = Octomate()
    app.config.artifacts.path = "/work/artifacts"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://console.example"
    ) as client:
        response = await client.get("/api/artifacts/config")
    assert response.status_code == 200
    assert response.json()["path"] == "/work/artifacts"
    assert response.headers["cache-control"] == "no-store"
