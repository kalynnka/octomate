from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.toolsets import FunctionToolset

inkling_toolset: FunctionToolset[None] = FunctionToolset()


@inkling_toolset.tool
async def ask_user(ctx: RunContext[None], question: str) -> str:
    """Ask the user a clarifying question and wait for their answer."""
    raise CallDeferred()
