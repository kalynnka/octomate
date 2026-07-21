"""Ask capability: the deferred ask-the-user tool.

A capability rather than a loose toolset entry so the caller decides per run
whether a user exists to ask at all — an interactive run mounts it, an
accomplice run simply never sees the tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.schemas.deferred import QuestionRequest

ASK_QUESTIONS_TOOL_NAME = "ask_questions"


@dataclass
class AskCapability(AbstractCapability[None]):
    """Offers `ask_questions` — the deferred ask-the-user batch."""

    toolset: FunctionToolset[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        toolset: FunctionToolset[None] = FunctionToolset(id="ask")

        @toolset.tool(name=ASK_QUESTIONS_TOOL_NAME)
        async def ask_questions(
            ctx: RunContext[None],
            questions: list[QuestionRequest],
        ) -> list[str]:
            """Ask the user several questions and wait for all answers as one batch."""
            if not questions:
                raise ValueError("ask_questions requires at least one question")
            raise CallDeferred(metadata={"kind": "questions"})

        self.toolset = toolset

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset
