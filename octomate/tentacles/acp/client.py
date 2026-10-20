"""Own local Claude and Codex ACP connections and identify their native sessions.

The installed adapters speak ACP over stdio. The SDK owns framing, request IDs,
concurrent callbacks and process shutdown. A caller handles agent requests and
notifications directly; the nerve is not a request/reply transport.

This is the client foundation, not a registered agent or a public API. It neither
creates managed workspaces nor persists transcripts. Authentication, session
access checks and ledger projection belong to the caller that exposes a session.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

import acp
from acp import schema
from acp.connection import Connection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.utils import request_model
from anyio import Path as AsyncPath
from pydantic import JsonValue

from octomate.schemas.thread import ThreadKey
from octomate.types.threads import CLAUDE_NATIVE_ID, CODEX_NATIVE_ID

type AcpClientHandler = Callable[
    [str, JsonValue | None, bool], Awaitable[JsonValue | None]
]


class AcpProvider(StrEnum):
    """The two native runtimes supported by this client."""

    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def command(self) -> str:
        """Installed adapter executable; connecting never installs a package."""
        return "claude-agent-acp" if self is AcpProvider.CLAUDE else "codex-acp"

    @property
    def native_id(self) -> str:
        """Use the same native surface as the runtime's transcript ingestion."""
        return CLAUDE_NATIVE_ID if self is AcpProvider.CLAUDE else CODEX_NATIVE_ID

    @staticmethod
    async def resolve_workspace(
        cwd: str,
        additional_directories: list[str] | None = None,
    ) -> tuple[str, list[str] | None]:
        """Resolve absolute server paths, including symbolic-link aliases."""
        if any(
            not AsyncPath(path).is_absolute()
            for path in [cwd, *(additional_directories or [])]
        ):
            raise ValueError("ACP workspace paths must be absolute")
        return (
            str(await AsyncPath(cwd).resolve()),
            [str(await AsyncPath(path).resolve()) for path in additional_directories]
            if additional_directories is not None
            else None,
        )


@dataclass(frozen=True)
class NativeAcpSession[
    ResponseT: schema.NewSessionResponse
    | schema.LoadSessionResponse
    | schema.ResumeSessionResponse
]:
    """A provider-owned session, retaining its ACP response and workspace.

    Claude's adapter uses its SDK session ID; Codex's uses its App Server thread
    ID. These identify the same native threads as their transcript streams.
    This binding does not itself register a thread or grant access to one.
    """

    provider: AcpProvider
    cwd: str  # Canonical server workspace path.
    session_id: str  # Native identity supplied by creation or an authorized reopen.
    response: ResponseT  # Includes provider modes and extensions.

    @property
    def key(self) -> ThreadKey:
        """The existing native thread identity, with the provider's session ID."""
        return ThreadKey(self.provider.native_id, "thread", self.session_id)


@dataclass(frozen=True)
class NativeAcpClient:
    """One initialized adapter connection, which may own several native sessions.

    Use the SDK connection for ACP requests and notifications. Keeping the typed
    request models at that boundary preserves `_meta` without flattening its keys
    into method arguments. The peer's initialization response remains available
    for capability negotiation before a caller exposes any optional operations.
    """

    provider: AcpProvider
    connection: Connection  # Live only inside the connect context.
    initialization: schema.InitializeResponse

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        provider: AcpProvider,
        handler: AcpClientHandler,
        initialize: schema.InitializeRequest,
        *,
        env: dict[str, str] | None = None,
    ) -> AsyncGenerator[Self]:
        """Start an installed adapter and negotiate ACP v1 before yielding.

        `handler` receives the method, JSON parameters and notification flag for
        agent-to-client traffic, including permissions. `initialize` declares
        only capabilities that handler implements. Environment overrides augment
        the SDK's minimal inherited environment. Stderr is inherited so an unread
        pipe cannot stall the adapter; stdout remains exclusively protocol traffic.

        Exiting closes this connection and its process, including on failed
        initialization or cancellation. It does not delete provider sessions.
        """
        if initialize.protocol_version != acp.PROTOCOL_VERSION:
            raise ValueError("The native ACP client requires protocol version 1")
        async with acp.spawn_stdio_connection(
            handler,
            provider.command,
            env=env,
            stderr=None,
            limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
        ) as (connection, _process):
            response = await request_model(
                connection,
                acp.AGENT_METHODS["initialize"],
                initialize,
                schema.InitializeResponse,
            )
            if response.protocol_version != initialize.protocol_version:
                raise ValueError(
                    f"ACP agent selected unsupported protocol {response.protocol_version}"
                )
            yield cls(provider, connection, response)

    async def new_session(
        self, request: schema.NewSessionRequest
    ) -> NativeAcpSession[schema.NewSessionResponse]:
        """Create a native session using the supplied workspace and MCP settings.

        The agent tentacle supplies the managed server workspace.
        HTTP MCP settings and extensions cross the SDK boundary intact, without
        mutating the caller's request. MCP process commands are refused.
        """
        if any(
            not isinstance(server, schema.HttpMcpServer)
            for server in request.mcp_servers
        ):
            raise ValueError("Only HTTP MCP servers are supported")
        cwd, additional_directories = await self.provider.resolve_workspace(
            request.cwd,
            request.additional_directories,
        )
        request = request.model_copy(
            update={"cwd": cwd, "additional_directories": additional_directories}
        )
        response = await request_model(
            self.connection,
            acp.AGENT_METHODS["session_new"],
            request,
            schema.NewSessionResponse,
        )
        return NativeAcpSession(
            self.provider, request.cwd, response.session_id, response
        )
