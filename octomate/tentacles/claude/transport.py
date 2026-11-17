"""Claude's native control protocol over a process inside a habitat."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

import anyio
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Transport
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk._version import __version__ as SDK_VERSION
from claude_agent_sdk.types import _configure_can_use_tool
from pydantic import TypeAdapter, ValidationError

from octomate.fibers import MAX_FRAME_BYTES, FiberError
from octomate.habitat import ManagedHabitat
from octomate.types.json import JsonObject

message_adapter = TypeAdapter(JsonObject)


@dataclass
class HabitatTransport(Transport):
    """A duplex SDK connection; the caller owns the guest process and the habitat."""

    stream: ByteStream
    max_frame_bytes: int = MAX_FRAME_BYTES
    reader: BufferedByteReceiveStream = field(init=False)
    ready: bool = field(default=False, init=False)
    input_ended: bool = field(default=False, init=False)
    writing: anyio.Lock = field(default_factory=anyio.Lock, init=False)

    def __post_init__(self) -> None:
        self.reader = BufferedByteReceiveStream(self.stream)

    async def connect(self) -> None:
        self.ready = True

    def is_ready(self) -> bool:
        return self.ready

    async def write(self, data: str) -> None:
        async with self.writing:
            await self.stream.send(data.encode())

    async def end_input(self) -> None:
        async with self.writing:
            await self.stream.send_eof()
            self.input_ended = True

    async def read_messages(self) -> AsyncIterator[JsonObject]:
        while True:
            try:
                frame = await self.reader.receive_until(b"\n", self.max_frame_bytes + 1)
            except anyio.IncompleteRead:
                if self.reader.buffer:
                    raise FiberError("Incomplete Claude protocol frame") from None
                if not self.input_ended:
                    raise FiberError("Claude protocol stream closed") from None
                return
            except anyio.DelimiterNotFound:
                raise FiberError(
                    "Claude protocol frame exceeds the size limit"
                ) from None
            if len(frame) > self.max_frame_bytes:
                raise FiberError("Claude protocol frame exceeds the size limit")
            try:
                yield message_adapter.validate_json(frame)
            except ValidationError:
                raise FiberError("Invalid Claude protocol frame") from None

    async def close(self) -> None:
        self.ready = False
        await self.stream.aclose()

    @classmethod
    @asynccontextmanager
    async def client(
        cls, habitat: ManagedHabitat, options: ClaudeAgentOptions
    ) -> AsyncGenerator[ClaudeSDKClient]:
        """Run the CLI in a habitat the caller holds; option paths refer to the guest."""
        if options.cwd is None:
            raise ValueError("Habitat Claude runs require a guest workspace")
        command_options = replace(
            _configure_can_use_tool(options),
            cli_path=options.cli_path or "claude",
        )
        # Reuse the pinned SDK's flag mapping for its native control protocol.
        command = SubprocessCLITransport("", command_options)._build_command()
        async with habitat.execute(
            *command,
            workdir=str(options.cwd),
            env={
                "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                "CLAUDE_AGENT_SDK_VERSION": SDK_VERSION,
                **options.env,
            },
        ) as stream:
            transport = cls(
                stream, max_frame_bytes=options.max_buffer_size or MAX_FRAME_BYTES
            )
            async with ClaudeSDKClient(options=options, transport=transport) as client:
                yield client
                # Keep the SDK reader alive until Claude finishes its exit hooks.
                if not transport.input_ended:
                    await transport.end_input()
                async for _ in client.receive_messages():
                    pass
