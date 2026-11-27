"""CLI-only OAuth grants, shared by hook processes and transcript tails."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import secrets
import tempfile
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import typer
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr

from octomate_cli.config import cli_settings, user_config_path

LOGIN_TIMEOUT = 300


class LoginRequired(RuntimeError):
    """The CLI needs a new user-approved grant."""


def origin(url: str) -> str:
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or (
            scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise ValueError("OAuth requires HTTPS, except on the loopback interface")
    return str(AnyHttpUrl(f"{scheme}://{parts.netloc}")).rstrip("/")


class OAuthMetadata(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str
    code_challenge_methods_supported: list[str]

    def validate_origin(self, server: str) -> None:
        if (
            self.issuer != f"{server}/"
            or "S256" not in self.code_challenge_methods_supported
        ):
            raise ValueError("The server's OAuth issuer or PKCE support does not match")
        for endpoint in (
            self.authorization_endpoint,
            self.token_endpoint,
            self.registration_endpoint,
            self.revocation_endpoint,
        ):
            if (
                origin(endpoint) != server
                or urlsplit(endpoint).query
                or urlsplit(endpoint).fragment
            ):
                raise ValueError("OAuth endpoints must belong to the configured server")


class TokenResponse(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    access_token: SecretStr
    refresh_token: SecretStr
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0)
    scope: str


class ClientRegistration(BaseModel):
    client_id: str


class LoginSession(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    issuer: str
    client_id: str
    metadata: OAuthMetadata
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: float
    scope: str


class LoginStore:
    def __init__(self, server: str) -> None:
        self.server: str = origin(server)
        self.path: Path = (
            user_config_path().parent
            / "oauth"
            / (hashlib.sha256(self.server.encode()).hexdigest() + ".json")
        )

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self) -> LoginSession | None:
        if not self.path.exists():
            return None
        session = LoginSession.model_validate_json(self.path.read_text())
        if session.issuer != self.server:
            raise ValueError("Stored OAuth credentials belong to another server")
        session.metadata.validate_origin(self.server)
        return session

    def write(self, session: LoginSession) -> None:
        document = session.model_dump(exclude={"access_token", "refresh_token"})
        document["access_token"] = session.access_token.get_secret_value()
        document["refresh_token"] = session.refresh_token.get_secret_value()
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle)
        try:
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def bearer(self) -> SecretStr | None:
        if not self.path.is_file():
            return None
        with self.lock():
            session = self.read()
            if session is None:
                return None
            if session.expires_at > time.time() + 30:
                return session.access_token
            with httpx2.Client(timeout=15) as client:
                response = client.post(
                    session.metadata.token_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": session.client_id,
                        "refresh_token": session.refresh_token.get_secret_value(),
                        "resource": f"{self.server}/api",
                    },
                )
            if response.status_code in {400, 401}:
                raise LoginRequired(
                    "CLI authorization expired or was revoked. Run `octomate logout`, then `octomate login`."
                )
            response.raise_for_status()
            tokens = TokenResponse.model_validate_json(response.content)
            session.access_token = tokens.access_token
            session.refresh_token = tokens.refresh_token
            session.expires_at = time.time() + tokens.expires_in
            session.scope = tokens.scope
            self.write(session)
            return session.access_token


def bearer(url: str, api_key: str | None) -> SecretStr | None:
    """Explicit API keys take precedence; OAuth credentials never cross origins."""
    if api_key:
        return SecretStr(api_key)
    return LoginStore(url).bearer()


def authorize(server: str, *, open_browser: bool = True) -> None:
    if (
        urlsplit(server).path not in {"", "/"}
        or urlsplit(server).query
        or urlsplit(server).fragment
    ):
        raise ValueError(
            "The OAuth server URL must be an origin without a path, query, or fragment"
        )
    store = LoginStore(server)
    with store.lock(), httpx2.Client(timeout=15) as client:
        if store.read() is not None:
            raise LoginRequired(
                "A CLI login already exists for this server. Run `octomate logout` before signing in again."
            )
        response = client.get(f"{store.server}/.well-known/oauth-authorization-server")
        response.raise_for_status()
        metadata = OAuthMetadata.model_validate_json(response.content)
        metadata.validate_origin(store.server)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        code: str | None = None
        declined = False

        class Callback(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(5)

            def do_GET(self) -> None:
                nonlocal code, declined
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                valid = (
                    parts.path == "/callback"
                    and all(len(values) == 1 for values in query.values())
                    and secrets.compare_digest(
                        query.get("state", [""])[0].encode(), state.encode()
                    )
                    and query.get("iss") == [metadata.issuer]
                    and (("code" in query) != ("error" in query))
                )
                if not valid:
                    self.send_error(400, "Invalid OAuth callback")
                    return
                code = query.get("code", [None])[0]
                declined = "error" in query
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(
                    b"Authorization received. Return to the terminal to finish signing in."
                )

            def log_message(self, format: str, *args: str | int) -> None:
                # Callback query strings contain the single-use authorization code.
                return

        with HTTPServer(("127.0.0.1", 0), Callback) as callback:
            callback.timeout = 1
            redirect = f"http://127.0.0.1:{callback.server_port}/callback"
            response = client.post(
                metadata.registration_endpoint,
                json={
                    "client_name": "Octomate CLI",
                    "redirect_uris": [redirect],
                    "scope": "hooks acp",
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            response.raise_for_status()
            registration = ClientRegistration.model_validate_json(response.content)
            url = (
                metadata.authorization_endpoint
                + "?"
                + urlencode(
                    {
                        "client_id": registration.client_id,
                        "redirect_uri": redirect,
                        "response_type": "code",
                        "scope": "hooks acp",
                        "resource": f"{store.server}/api",
                        "state": state,
                        "code_challenge": base64.urlsafe_b64encode(
                            hashlib.sha256(verifier.encode()).digest()
                        )
                        .decode()
                        .rstrip("="),
                        "code_challenge_method": "S256",
                    }
                )
            )
            typer.echo(f"Authorize this CLI in your browser:\n{url}")
            if open_browser:
                webbrowser.open(url)
            deadline = time.monotonic() + LOGIN_TIMEOUT
            while code is None and not declined and time.monotonic() < deadline:
                callback.handle_request()
            if code is None and not declined:
                raise LoginRequired(
                    "Login timed out. Run `octomate login` to try again."
                )
            if declined:
                raise LoginRequired("CLI authorization was declined.")
            response = client.post(
                metadata.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": registration.client_id,
                    "code": code,
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                    "resource": f"{store.server}/api",
                },
            )
            response.raise_for_status()
            tokens = TokenResponse.model_validate_json(response.content)
            store.write(
                LoginSession(
                    issuer=store.server,
                    client_id=registration.client_id,
                    metadata=metadata,
                    access_token=tokens.access_token,
                    refresh_token=tokens.refresh_token,
                    expires_at=time.time() + tokens.expires_in,
                    scope=tokens.scope,
                )
            )


def login(
    url: Annotated[
        str | None, typer.Option(help="Server origin; defaults to the configured URL.")
    ] = None,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser/--no-browser", help="Open the authorization URL automatically."
        ),
    ] = True,
) -> None:
    """Authorize this CLI independently of each native harness's MCP login."""
    server = url or cli_settings().url
    if server is None:
        raise typer.BadParameter(
            "Provide --url or run `octomate configure --url <server>` first."
        )
    try:
        authorize(server, open_browser=browser)
    except (LoginRequired, ValueError, httpx2.HTTPError, OSError) as error:
        raise typer.BadParameter(str(error)) from None
    typer.echo(f"Signed in to {origin(server)} for CLI hooks and ACP.")
    if cli_settings().token:
        typer.echo("The configured API key still takes precedence for requests.")


def logout(
    url: Annotated[
        str | None, typer.Option(help="Server origin; defaults to the configured URL.")
    ] = None,
) -> None:
    """Revoke this CLI's grant and remove its stored credentials."""
    server = url or cli_settings().url
    if server is None:
        raise typer.BadParameter("Provide --url or configure a server URL first.")
    try:
        store = LoginStore(server)
        with store.lock():
            session = store.read()
            if session is not None:
                with httpx2.Client(timeout=15) as client:
                    response = client.post(
                        session.metadata.revocation_endpoint,
                        data={
                            "client_id": session.client_id,
                            "token": session.refresh_token.get_secret_value(),
                        },
                    )
                response.raise_for_status()
                store.path.unlink()
    except (ValueError, httpx2.HTTPError, OSError) as error:
        raise typer.BadParameter(str(error)) from None
    typer.echo(f"Signed out of {origin(server)}.")
