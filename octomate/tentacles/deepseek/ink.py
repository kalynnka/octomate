"""Typed operations over the tentacle's shared DSH Remote client."""

from dataclasses import dataclass
from types import TracebackType
from typing import Self

from octomate_protocol.deepseek import ErrResult
from pydantic_ai.exceptions import AgentRunError

from octomate.tentacles.deepseek.catalog import (
    DeepseekCommandDescriptor,
    command_descriptors_adapter,
)
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.wire import CommandExecutionValue


@dataclass
class DeepseekInk:
    """Own the Remote client context and native catalog inspection, without managers."""

    client: DeepseekApiClient

    async def __aenter__(self) -> Self:
        await self.client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.client.__aexit__(exc_type, exc_value, traceback)

    async def list_commands(self, session_id: str) -> list[DeepseekCommandDescriptor]:
        """Inspect the exact receiving agent without creating or prompting a session."""
        result = await self.client.remote("commands/list", {"agentId": session_id})
        if isinstance(result, ErrResult):
            raise AgentRunError(
                f"dsh commands/list failed: {result.error.message} ({result.error.code})"
            )
        return command_descriptors_adapter.validate_python(result.value)

    async def execute_command(
        self, session_id: str, line: str
    ) -> CommandExecutionValue | None:
        """Submit an exact command line without attachments.

        None means no command matched. Matched success and command errors retain
        their native outcome and lifecycle identity; Remote failures raise.
        """
        result = await self.client.remote(
            "commands/execute",
            {"agentId": session_id, "line": line, "submittedAttachments": []},
        )
        if isinstance(result, ErrResult):
            raise AgentRunError(
                f"dsh commands/execute failed: {result.error.message} ({result.error.code})"
            )
        if result.value is None:
            return None
        return CommandExecutionValue.model_validate(result.value)
