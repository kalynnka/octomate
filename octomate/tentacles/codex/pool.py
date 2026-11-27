"""Retained Codex connections, notification readers, and thread routing."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
from functools import partial
from pathlib import PurePosixPath

import anyio
from openai_codex import CodexConfig

from octomate.habitat import Habitat, ManagedHabitat, WorkspaceHabitat
from octomate.telemetry import codex_logfire
from octomate.tentacles.codex.client import RequestHandler, SharedCodex
from octomate.types.json import JsonObject


class CodexClientPool:
    """Own one core client and one client per managed habitat."""

    core_client: SharedCodex
    habitat_clients: dict[str, SharedCodex]  # Indexed by habitat name.
    thread_routes: dict[
        str, SharedCodex
    ]  # References to clients, including closed ones.
    notification_readers: dict[SharedCodex, asyncio.Task[None]]
    creation_lock: asyncio.Lock
    guest_binary: str
    instrument: bool
    handle_request: RequestHandler
    watch_notifications: Callable[[SharedCodex], Coroutine[None, None, None]] | None

    def __init__(
        self,
        config: CodexConfig,
        *,
        handler: RequestHandler,
        instrument: bool,
        handle_request: RequestHandler,
    ) -> None:
        self.core_client = SharedCodex(config, handler=handler, instrument=instrument)
        self.habitat_clients = {}
        self.thread_routes = {}
        self.notification_readers = {}
        self.creation_lock = asyncio.Lock()
        self.guest_binary = config.codex_bin or "codex"
        self.instrument = instrument
        self.handle_request = handle_request
        self.watch_notifications = None
        self.core_client.transport.thread_opened = partial(
            self.bind_thread, self.core_client
        )

    @property
    def running(self) -> bool:
        reader = self.notification_readers.get(self.core_client)
        return reader is not None and not reader.done()

    def bind_thread(self, client: SharedCodex, thread_id: str) -> None:
        """Bind a thread to its selected client, rejecting another live owner."""
        previous = self.thread_routes.get(thread_id)
        if (
            previous is not None
            and previous is not client
            and not previous.transport.closing
        ):
            raise ValueError("Codex thread is already open in another habitat")
        self.thread_routes[thread_id] = client

    async def start(
        self,
        *,
        watch_notifications: Callable[[SharedCodex], Coroutine[None, None, None]],
    ) -> None:
        self.watch_notifications = watch_notifications
        self.notification_readers[self.core_client] = await self.start_client(
            self.core_client
        )

    async def start_client(self, client: SharedCodex) -> asyncio.Task[None]:
        if self.watch_notifications is None:
            raise RuntimeError("Codex runtime is not running")
        starting = asyncio.create_task(client.__aenter__())
        try:
            await asyncio.shield(starting)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await asyncio.gather(starting, return_exceptions=True)
                await client.close()
            raise
        return asyncio.create_task(self.watch_notifications(client))

    async def close(self) -> None:
        # Closing the transports releases the SDKs' blocking notification reads.
        readers = list(self.notification_readers.values())
        self.notification_readers.clear()
        await asyncio.gather(
            self.core_client.close(),
            *(client.close() for client in self.habitat_clients.values()),
        )
        await asyncio.gather(*readers)
        self.habitat_clients.clear()
        self.thread_routes.clear()

    @codex_logfire.instrument("codex.acquire", extract_args=["thread_id"])
    async def acquire(
        self,
        habitat: Habitat | None = None,
        *,
        env: dict[str, str] | None = None,
        thread_id: str | None = None,
    ) -> SharedCodex:
        """Select an existing thread's client, a habitat client, or core discovery."""
        if habitat is None and thread_id is not None:
            return self.thread_routes.get(thread_id, self.core_client)
        if not isinstance(habitat, ManagedHabitat):
            client = self.core_client
            if thread_id is not None:
                self.bind_thread(client, thread_id)
            return client
        if self.watch_notifications is None:
            raise RuntimeError("Codex runtime is not running")
        async with self.creation_lock:
            existing = self.habitat_clients.get(habitat.id)
            codex_logfire.info(
                "Acquire Codex client in {habitat}",
                habitat=habitat.id,
                reused=existing is not None and not existing.transport.closing,
                thread_id=thread_id,
            )
            if existing is not None and not existing.transport.closing:
                if thread_id is not None:
                    self.bind_thread(existing, thread_id)
                return existing
            if existing is not None:
                await self.notification_readers[existing]
            # The config volume outlives each invocation's workspace checkout.
            if env is None:
                raise ValueError("A new habitat client requires its environment")
            cwd = str(PurePosixPath(env["CODEX_HOME"]).parent)
            if isinstance(habitat, WorkspaceHabitat):
                home = anyio.Path(env["CODEX_HOME"])
                await home.mkdir(parents=True, exist_ok=True)
                await (home / "environments.toml").write_text("include_local = false\n")
            config = replace(
                self.core_client.transport.config,
                codex_bin=self.core_client.transport.config.codex_bin
                if isinstance(habitat, WorkspaceHabitat)
                else self.guest_binary,
                cwd=cwd,
                env={**(self.core_client.transport.config.env or {}), **env},
            )

            async def handle(method: str, params: JsonObject | None) -> JsonObject:
                requested_thread = (params or {}).get("threadId")
                if (
                    not isinstance(requested_thread, str)
                    or self.thread_routes.get(requested_thread) is not client
                ):
                    raise ValueError(
                        "Codex request belongs to no thread in this habitat"
                    )
                return await self.handle_request(method, params)

            client = SharedCodex(
                config,
                handler=handle,
                instrument=self.instrument,
                habitat=habitat,
            )
            client.transport.thread_opened = partial(self.bind_thread, client)
            self.notification_readers[client] = await self.start_client(client)
            self.habitat_clients[habitat.id] = client
            if existing is not None:
                self.notification_readers.pop(existing)
            if thread_id is not None:
                self.bind_thread(client, thread_id)
            return client
