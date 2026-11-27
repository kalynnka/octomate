"""Claude SDK client lifecycle, initialization and native message streaming."""

import asyncio
import contextlib
import json
import os
import uuid
import weakref
from collections.abc import AsyncGenerator, Callable
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, gettempdir

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionMode,
    fork_session,
    get_session_messages,
)
from claude_agent_sdk.types import Message

from octomate.habitat import Habitat, HabitatError, ManagedHabitat
from octomate.habitat.srt import SrtHabitat
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.tentacles.agent import AgentInk
from octomate.tentacles.claude.catalog import (
    CONTROL_COMMANDS,
    ClaudeCommandDescriptor,
    ClaudeServerInfo,
)
from octomate.tentacles.claude.transcript import (
    CompactBoundary,
    TranscriptUserMessage,
    transcripts_dir,
)
from octomate.tentacles.claude.transcript import (
    relocate_session as relocate_session_files,
)
from octomate.tentacles.claude.transport import HabitatTransport, workspace


class ClaudeInk(AgentInk):
    """Own turn-local SDK clients without depending on Octomate managers.

    Options and turn callbacks arrive at the operation that uses them. The
    tentacle keeps workspace ownership, approval decisions and persistence.
    """

    live_clients: weakref.WeakValueDictionary[
        uuid.UUID, ClaudeSDKClient
    ]  # Conversation IDs distinguish sibling subagents sharing a thread.

    def __init__(self) -> None:
        self.live_clients = weakref.WeakValueDictionary()

    async def fork_session(
        self,
        session_id: str | None,
        *,
        cwd: Path | PurePosixPath,
        habitat: Habitat,
        env: dict[str, str] | None = None,
        transcript: bytes | None = None,
    ) -> str:
        """Create a durable SDK fork in the destination's session storage.

        Managed habitats run the offline SDK helper beside their session files.
        `transcript` supplies retained history through its last completed turn.
        Ink owns staging and transport for either execution environment.
        """

        if not session_id:
            raise ValueError("Cannot fork a Claude conversation without a session id")
        if isinstance(habitat, ManagedHabitat):
            args = ("--transcript",) if transcript is not None else ()
            async with habitat.resident(
                "octomate",
                "claude",
                "habitat",
                "relocate",
                session_id,
                *args,
                workdir=str(cwd),
                env=env,
            ) as stream:
                if transcript is not None:
                    await stream.send(transcript)
                await stream.send_eof()
                try:
                    line = await BufferedByteReceiveStream(stream).receive_until(
                        b"\n", 37
                    )
                    return str(uuid.UUID(line.decode("ascii")))
                except (ValueError, anyio.IncompleteRead, anyio.DelimiterNotFound):
                    raise HabitatError(
                        "Claude did not return a forked session ID"
                    ) from None

        def fork() -> str:
            if transcript is None:
                forked = fork_session(session_id)
                relocate_session_files(forked.session_id, cwd=Path(cwd))
                return forked.session_id
            staged = transcripts_dir(Path(cwd)) / f"{session_id}.jsonl"
            staged.parent.mkdir(parents=True, exist_ok=True)
            with staged.open("xb") as output:
                try:
                    output.write(transcript)
                    output.flush()
                    return fork_session(session_id, directory=str(cwd)).session_id
                finally:
                    staged.unlink()

        return await asyncio.to_thread(fork)

    async def relocate_session(
        self, session_id: str | None, *, cwd: Path | PurePosixPath, habitat: Habitat
    ) -> None:
        """Place a session in its next workspace; managed forks already land there."""
        if session_id is not None and not isinstance(habitat, ManagedHabitat):
            await asyncio.to_thread(relocate_session_files, session_id, cwd=Path(cwd))

    async def inspect(self, options: ClaudeAgentOptions) -> ClaudeServerInfo:
        """Read a fresh initialization snapshot without submitting a prompt."""
        info = None
        async with ClaudeSDKClient(options=options) as client:
            info = await client.get_server_info()
        return ClaudeServerInfo.model_validate(info)

    async def compaction_messages(
        self, boundary: CompactBoundary, *, cwd: Path
    ) -> list[ModelRequest | ModelResponse]:
        """Read the exact summary UUID from a local driven session's SDK history."""
        summary_id = boundary.summary_uuid
        if summary_id is None:
            return []
        messages = await asyncio.to_thread(
            get_session_messages, boundary.session_id, directory=str(cwd)
        )
        summary = next(
            (
                message
                for message in messages
                if message.uuid == summary_id
                and message.session_id == boundary.session_id
                and message.type == "user"
            ),
            None,
        )
        if summary is None:
            return []
        message = TranscriptUserMessage.model_validate(summary.message)
        return [message.compaction_message(summary.uuid, boundary.timestamp)]

    async def stream(
        self,
        prompt: str,
        *,
        options: ClaudeAgentOptions,
        habitat: Habitat,
        conversation_id: uuid.UUID,
        should_interrupt: Callable[[], bool],
        command: ClaudeCommandDescriptor | None = None,
    ) -> AsyncGenerator[Message]:
        """Stream a turn, superseding the prior client for this conversation.

        The CLI starts inside a managed habitat, with paths as the guest sees
        them; otherwise it is a local process confined by Claude's own sandbox.
        """
        if options.cwd is None:
            raise ValueError("Driven Claude runs require a workspace")
        options.setting_sources = []
        async with contextlib.AsyncExitStack() as resources:
            if isinstance(habitat, SrtHabitat):
                habitat, options = await resources.enter_async_context(
                    workspace(habitat, options)
                )
            if isinstance(habitat, ManagedHabitat):
                options.sandbox = {
                    **(options.sandbox or {}),
                    "enabled": False,
                }
                client = await resources.enter_async_context(
                    HabitatTransport.client(habitat, options)
                )
            else:
                # Claude falls back to shared temp storage when this path is too long.
                temp = resources.enter_context(
                    TemporaryDirectory(prefix="octo-", dir="/tmp")
                )
                temp_root = str(await asyncio.to_thread(Path(temp).resolve))
                options.env["CLAUDE_CODE_TMPDIR"] = temp_root
                # These native filesystem settings are not in the SDK types yet.
                options.settings = json.dumps(
                    {
                        "permissions": {"blockReadsOutsideWorkingDirectories": True},
                        "sandbox": {
                            "enabled": True,
                            "failIfUnavailable": True,
                            "autoAllowBashIfSandboxed": False,
                            "allowUnsandboxedCommands": False,
                            "excludedCommands": [],
                            "enableWeakerNestedSandbox": False,
                            "filesystem": {
                                "denyRead": ["/tmp", gettempdir()],
                                "allowRead": [str(options.cwd), temp_root],
                                "allowWrite": [temp_root],
                            },
                        },
                    }
                )
                # The SDK merges env into the parent environment; empty values scrub it.
                options.env.update(
                    {
                        key: ""
                        for key in os.environ
                        if key.upper().startswith("OCTOMATE_")
                    }
                )
                client = await resources.enter_async_context(
                    ClaudeSDKClient(options=options)
                )
            if command is not None:
                info = ClaudeServerInfo.model_validate(await client.get_server_info())
                commands = {entry.id: entry for entry in info.commands or ()}
                commands.update(CONTROL_COMMANDS)
                if commands.get(command.id) != command:
                    raise LookupError("This command changed; refresh commands.")
            previous = self.live_clients.get(conversation_id)
            self.live_clients[conversation_id] = client
            resources.callback(
                lambda: (
                    self.live_clients.pop(conversation_id, None)
                    if self.live_clients.get(conversation_id) is client
                    else None
                )
            )
            if previous is not None and previous is not client:
                with contextlib.suppress(Exception):
                    await previous.interrupt()
            await client.query(prompt)
            interrupted = False
            async for message in client.receive_response():
                yield message
                if not interrupted and should_interrupt():
                    interrupted = True
                    await client.interrupt()

    async def set_permission_mode(
        self, conversation_id: uuid.UUID, mode: PermissionMode
    ) -> None:
        """Apply a control request to this conversation's active SDK client."""
        client = self.live_clients.get(conversation_id)
        if client is not None:
            await client.set_permission_mode(mode)

    async def close(self) -> None:
        """Interrupt active turns so their client contexts can close."""
        for client in self.live_clients.values():
            with contextlib.suppress(Exception):
                await client.interrupt()
        self.live_clients.clear()
