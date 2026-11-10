"""The model-history boundary between persisted rows and harness messages."""

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessagesTypeAdapter


class History(AbstractCapability[None]):
    """Give the harness message values without database relationships to copy."""

    async def before_run(self, ctx: RunContext[None]) -> None:
        if ctx.messages:
            ctx.messages[:] = ModelMessagesTypeAdapter.validate_python(
                [vars(message) for message in ctx.messages]
            )
