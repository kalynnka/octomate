from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.toolsets import FunctionToolset

from octomate.schemas.deferred import QuestionRequest

inkling_toolset: FunctionToolset[None] = FunctionToolset()


@inkling_toolset.tool
async def ask_questions(
    ctx: RunContext[None],
    questions: list[QuestionRequest],
) -> list[str]:
    """Ask the user several questions and wait for all answers as one batch."""
    if not questions:
        raise ValueError("ask_questions requires at least one question")
    raise CallDeferred(metadata={"kind": "questions"})
