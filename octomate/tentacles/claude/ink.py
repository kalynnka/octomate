"""Claude SDK client lifecycle, initialization and native message streaming."""

import asyncio
import contextlib
import uuid
import weakref
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionMode,
    fork_session,
)
from claude_agent_sdk.types import Message

from octomate.tentacles.claude.catalog import ClaudeCommandDescriptor, ClaudeServerInfo
from octomate.tentacles.claude.transcript import relocate_session


class ClaudeInk:
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
        self, session_id: str, *, cwd: Path, source_cwd: Path | None = None
    ) -> str:
        """Fork with the SDK, then file the copy where the destination resumes it.

        The SDK's directory argument selects the source, not the destination.
        Native imports stage their source in the destination directory already.
        """

        def fork() -> str:
            forked = fork_session(
                session_id,
                directory=str(source_cwd) if source_cwd is not None else None,
            )
            if source_cwd != cwd:
                relocate_session(forked.session_id, cwd=cwd)
            return forked.session_id

        return await asyncio.to_thread(fork)

    async def inspect(self, options: ClaudeAgentOptions) -> ClaudeServerInfo:
        """Read a fresh initialization snapshot without submitting a prompt."""
        info = None
        async with ClaudeSDKClient(options=options) as client:
            info = await client.get_server_info()
        return ClaudeServerInfo.model_validate(info)

    async def stream(
        self,
        prompt: str,
        *,
        options: ClaudeAgentOptions,
        conversation_id: uuid.UUID,
        should_interrupt: Callable[[], bool],
        command: ClaudeCommandDescriptor | None = None,
    ) -> AsyncGenerator[Message]:
        """Stream a turn, superseding the prior client for this conversation."""
        async with contextlib.AsyncExitStack() as resources:
            client = await resources.enter_async_context(
                ClaudeSDKClient(options=options)
            )
            if command is not None:
                info = ClaudeServerInfo.model_validate(await client.get_server_info())
                commands = {entry.id: entry for entry in info.commands or ()}
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
