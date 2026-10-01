"""Claude SDK client lifecycle, initialization and native message streaming."""

import contextlib
import uuid
import weakref
from collections.abc import AsyncGenerator, Callable

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import Message

from octomate.tentacles.claude.catalog import ClaudeCommandDescriptor, ClaudeServerInfo


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
        async with ClaudeSDKClient(options=options) as client:
            if command is not None:
                info = ClaudeServerInfo.model_validate(await client.get_server_info())
                commands = {entry.id: entry for entry in info.commands or ()}
                if commands.get(command.id) != command:
                    raise LookupError("This command changed; refresh commands.")
            previous = self.live_clients.get(conversation_id)
            self.live_clients[conversation_id] = client
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

    async def close(self) -> None:
        """Interrupt active turns so their client contexts can close."""
        for client in self.live_clients.values():
            with contextlib.suppress(Exception):
                await client.interrupt()
        self.live_clients.clear()
