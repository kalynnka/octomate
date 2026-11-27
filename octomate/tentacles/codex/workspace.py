"""Run-scoped Codex execution behind SRT while its agent loop stays on core."""

import asyncio
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from tempfile import TemporaryDirectory

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream
from codex_cli_bin import bundled_codex_path
from openai_codex.api import Sandbox
from openai_codex.generated.v2_all import LegacyAppPathString, TurnEnvironmentParams
from uuid_utils.compat import uuid7
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from octomate.habitat.srt import SrtHabitat
from octomate.managers.habitat import Invocation
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.codex.schemas import EnvironmentAddResponse


@asynccontextmanager
async def execution(
    client: SharedCodex, invocation: Invocation, sandbox: Sandbox
) -> AsyncGenerator[TurnEnvironmentParams]:
    """Expose only this invocation's executor, then close its listener and processes."""
    owner = invocation.habitat
    if not isinstance(owner, SrtHabitat):
        raise ValueError("Codex workspace execution requires an SRT habitat")
    binary = Path(
        await anyio.Path(
            client.transport.config.codex_bin or bundled_codex_path()
        ).resolve()
    )
    with TemporaryDirectory(prefix="octomate-codex-") as scratch:
        habitat = SrtHabitat(
            config=owner.config.model_copy(
                update={"read_paths": [*owner.config.read_paths, binary.parent]}
            ),
            user_id=owner.user_id,
            root=Path(invocation.cwd),
            name=owner.name,
            read_only=sandbox == Sandbox.read_only,
            scratch=Path(scratch),
        )
        token = token_urlsafe(32)

        def authenticate(
            connection: ServerConnection, request: Request
        ) -> Response | None:
            if not compare_digest(
                request.headers.get("Authorization", ""), f"Bearer {token}"
            ):
                return connection.respond(
                    HTTPStatus.UNAUTHORIZED, "Invalid executor token"
                )
            return None

        async def relay(connection: ServerConnection) -> None:
            async with habitat.resident(
                str(binary),
                "exec-server",
                "--listen",
                "stdio",
                workdir=str(invocation.cwd),
            ) as stream:
                reader = BufferedByteReceiveStream(stream)

                async def incoming() -> None:
                    try:
                        async for message in connection:
                            data = (
                                message.encode()
                                if isinstance(message, str)
                                else message
                            )
                            await stream.send(data + b"\n")
                    except ConnectionClosed:
                        return

                async def outgoing() -> None:
                    try:
                        while True:
                            data = await reader.receive_until(b"\n", sys.maxsize)
                            await connection.send(data.decode())
                    except (anyio.EndOfStream, ConnectionClosed):
                        return

                tasks = [
                    asyncio.create_task(incoming()),
                    asyncio.create_task(outgoing()),
                ]
                try:
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await habitat.prepare()
            async with serve(
                relay,
                "127.0.0.1",
                0,
                process_request=authenticate,
                max_size=None,
            ) as server:
                port = next(iter(server.sockets)).getsockname()[1]
                # Loaded threads cache connections by identity, even after environment/add.
                environment_id = str(uuid7())
                await client._client.request(
                    "environment/add",
                    {
                        "environmentId": environment_id,
                        "execServerUrl": f"ws://127.0.0.1:{port}",
                        "authBearerToken": token,
                    },
                    response_model=EnvironmentAddResponse,
                )
                yield TurnEnvironmentParams(
                    environment_id=environment_id,
                    cwd=LegacyAppPathString(str(invocation.cwd)),
                )
        finally:
            with anyio.CancelScope(shield=True):
                await habitat.stop()
