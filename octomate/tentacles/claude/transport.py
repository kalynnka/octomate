"""Claude's native control protocol over a process inside a habitat."""

import logging
import shutil
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio
from anyio import to_thread
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Transport
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk._version import __version__ as SDK_VERSION
from claude_agent_sdk.types import _configure_can_use_tool
from opentelemetry import propagate
from pydantic import TypeAdapter, ValidationError

from octomate.fibers import MAX_FRAME_BYTES, FiberError
from octomate.habitat import HabitatError, ManagedHabitat
from octomate.habitat.srt import SrtHabitat
from octomate.types.json import JsonObject

message_adapter = TypeAdapter(JsonObject)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def workspace(
    owner: SrtHabitat, options: ClaudeAgentOptions
) -> AsyncGenerator[tuple[SrtHabitat, ClaudeAgentOptions]]:
    """Confine the whole CLI to its workspace while retaining Claude session state.

    Native tool permissions remain Claude's responsibility. This outer policy
    also confines its file tools and children without nesting its Bash sandbox.
    """
    if options.cwd is None:
        raise ValueError("SRT Claude runs require a workspace")
    config = options.env.get("CLAUDE_CONFIG_DIR")
    if not config:
        raise HabitatError("SRT Claude runs require an owner session directory")
    state = Path(await anyio.Path(config).resolve())
    if not state.is_relative_to(owner.root):
        raise HabitatError("Claude session storage must belong to the habitat owner")
    if options.cli_path is None:
        binary = await to_thread.run_sync(SubprocessCLITransport("", options)._find_cli)
    else:
        binary = await to_thread.run_sync(shutil.which, str(options.cli_path))
        if binary is None:
            raise HabitatError("Configured Claude executable was not found")
    executable = Path(await anyio.Path(binary).resolve())
    with TemporaryDirectory(prefix="octo-", dir="/tmp") as scratch:
        habitat = SrtHabitat(
            config=owner.config.model_copy(
                update={"read_paths": [*owner.config.read_paths, executable.parent]}
            ),
            user_id=owner.user_id,
            root=Path(options.cwd),
            name=owner.name,
            scratch=Path(scratch),
            state_paths=(state,),
        )
        runtime = habitat.environment()
        cache = Path(runtime["HOME"]) / ".cache"
        scoped = replace(
            options,
            cli_path=str(executable),
            env={
                **options.env,
                "HOME": runtime["HOME"],
                "TMPDIR": runtime["TMPDIR"],
                "CLAUDE_CODE_TMPDIR": runtime["TMPDIR"],
                "UV_CACHE_DIR": str(cache / "uv"),
                "npm_config_cache": str(cache / "npm"),
                "npm_config_store_dir": str(cache / "pnpm"),
            },
        )
        try:
            await anyio.Path(state).mkdir(parents=True, exist_ok=True)
            await habitat.prepare()
            yield habitat, scoped
        finally:
            with anyio.CancelScope(shield=True):
                await habitat.stop()


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
        env = {
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            **options.env,
            "CLAUDE_AGENT_SDK_VERSION": SDK_VERSION,
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "PWD": str(options.cwd),
        }
        env.pop("CLAUDE_AGENT_SDK_DERIVED_CLIENT_APP", None)
        if options.client_app is not None:
            env["CLAUDE_AGENT_SDK_CLIENT_APP"] = options.client_app
        if options.enable_file_checkpointing:
            env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"
        carrier: dict[str, str] = {}
        try:
            propagate.inject(carrier)
        except Exception:
            logger.debug("Claude trace context propagation failed", exc_info=True)
        if "traceparent" in carrier:
            env.update(
                (key.upper(), value)
                for key, value in carrier.items()
                if key.upper() not in options.env
            )

        def on_stderr(line: str) -> None:
            line = line.rstrip()
            if line and options.stderr is not None:
                try:
                    options.stderr(line)
                except Exception:
                    logger.debug("Claude stderr callback failed", exc_info=True)

        async with habitat.resident(
            *command,
            workdir=str(options.cwd),
            env=env,
            stderr=on_stderr if options.stderr is not None else None,
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
