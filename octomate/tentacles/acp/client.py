"""Own installed ACP harness connections and their provider sessions.

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
from typing import Self

import acp
from acp import schema
from acp.connection import Connection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.utils import request_model
from pydantic import JsonValue

from octomate.config.acp import AcpProvider

type AcpClientHandler = Callable[
    [str, JsonValue | None, bool], Awaitable[JsonValue | None]
]


@dataclass(frozen=True)
class NativeAcpSession[
    ResponseT: schema.NewSessionResponse
    | schema.LoadSessionResponse
    | schema.ResumeSessionResponse
]:
    """A provider-owned session, retaining its ACP response and workspace.

    The session ID is opaque and scoped to its configured harness. This binding
    does not itself register an Octomate thread or grant access to one.
    """

    provider: AcpProvider
    cwd: str  # Canonical server workspace path.
    session_id: str  # Native identity supplied by creation or an authorized reopen.
    response: ResponseT  # Includes provider modes and extensions.

    @property
    def model_option(self) -> schema.SessionConfigOptionSelect | None:
        """The first advertised model selector, including uncategorized `model`."""
        return next(
            (
                option
                for option in self.response.config_options or []
                if isinstance(option, schema.SessionConfigOptionSelect)
                and (option.category == "model" or option.id == "model")
            ),
            None,
        )

    @property
    def models(self) -> list[schema.SessionConfigSelectOption]:
        """Flatten the protocol's grouped or ungrouped model choices."""
        option = self.model_option
        return (
            [
                value
                for entry in option.options
                for value in (
                    entry.options
                    if isinstance(entry, schema.SessionConfigSelectGroup)
                    else [entry]
                )
            ]
            if option is not None
            else []
        )

    @property
    def effort_option(self) -> schema.SessionConfigOptionSelect | None:
        """The first advertised reasoning selector, using its semantic category."""
        return next(
            (
                option
                for option in self.response.config_options or []
                if isinstance(option, schema.SessionConfigOptionSelect)
                and option.category == "thought_level"
            ),
            None,
        )

    @property
    def efforts(self) -> tuple[str, ...]:
        """The current model's reasoning levels, in the provider's order."""
        option = self.effort_option
        return (
            tuple(
                value.value
                for entry in option.options
                for value in (
                    entry.options
                    if isinstance(entry, schema.SessionConfigSelectGroup)
                    else [entry]
                )
            )
            if option is not None
            else ()
        )

    async def select_model(
        self, connection: Connection, model: str | None
    ) -> str | None:
        """Validate against this session's choices before changing its model."""
        option = self.model_option
        if option is None:
            if model not in (None, "default"):
                raise ValueError("The ACP provider has no model selector")
            return None
        if model is None:
            return option.current_value
        if model not in {value.value for value in self.models}:
            raise ValueError(f"The ACP session does not offer model {model!r}")
        if model != option.current_value:
            response = await request_model(
                connection,
                acp.AGENT_METHODS["session_set_config_option"],
                schema.SetSessionConfigOptionSelectRequest(
                    session_id=self.session_id, config_id=option.id, value=model
                ),
                schema.SetSessionConfigOptionResponse,
            )
            self.response.config_options = response.config_options
        return model

    async def select_effort(self, connection: Connection, effort: str | None) -> None:
        """Apply an explicit effort after model selection has refreshed its choices.

        Omitting effort preserves the provider session's current setting.
        """
        if effort is None:
            return
        option = self.effort_option
        if option is None or effort not in self.efforts:
            raise ValueError(f"The ACP session does not offer effort {effort!r}")
        if effort != option.current_value:
            response = await request_model(
                connection,
                acp.AGENT_METHODS["session_set_config_option"],
                schema.SetSessionConfigOptionSelectRequest(
                    session_id=self.session_id, config_id=option.id, value=effort
                ),
                schema.SetSessionConfigOptionResponse,
            )
            self.response.config_options = response.config_options


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
            *provider.args,
            env={
                **{
                    name: value.get_secret_value()
                    for name, value in provider.env.items()
                },
                **(env or {}),
            },
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
            if provider.auth_method is not None:
                if provider.auth_method not in {
                    method.id for method in response.auth_methods or []
                }:
                    raise ValueError(
                        "The ACP provider does not offer this authentication method"
                    )
                await request_model(
                    connection,
                    acp.AGENT_METHODS["authenticate"],
                    schema.AuthenticateRequest(method_id=provider.auth_method),
                    schema.AuthenticateResponse,
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
