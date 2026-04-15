from __future__ import annotations

from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.toolsets import FunctionToolset

from octomate.tentacles.agent.context import SessionContext
from octomate.schemas.segments import TextSegment


def channel_toolset() -> FunctionToolset[SessionContext]:
    toolset: FunctionToolset[SessionContext] = FunctionToolset()

    @toolset.tool
    async def acknowledge(ctx: RunContext[SessionContext], text: str) -> str:
        """
        Send a short message to the user immediately before doing heavy work or
        provide extra info ahead of next steps to inform the user what is going on.

        Call this FIRST when about to invoke a skill or tool that may take a few
        seconds (e.g. weather, search, knowledge base), so the user knows you are
        working on it. Do NOT use for simple replies like greetings or responses
        that don't involve tool calls. Example: acknowledge("let me look that up~")
        """
        if ctx.deps.tentacle:
            await ctx.deps.tentacle.twitch(
                ctx.deps.session_key, [TextSegment(data={"text": text})]
            )
        return "acknowledged"

    @toolset.tool
    async def ask_user(
        ctx: RunContext[SessionContext],
        question: str,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user a question and wait for their answer before continuing.

        ALWAYS USE this tool when you need clarification or a decision from the user.
        DO NOT SEND a separate text message asking the same thing — this tool handles it.
        Provide options to show choice buttons; omit for free-text input
        (platform-dependent — may not be supported everywhere).
        Returns the user's answer, or '(no response)' on timeout.
        """
        raise CallDeferred()

    @toolset.tool
    async def create_todo(ctx: RunContext[SessionContext], title: str) -> str:
        """Create a TODO card for the user in the current chat.

        Use this whenever a task has multiple stages or steps — create a todo item
        for each stage so the user can track progress. Returns a todo ID on success,
        or an error message if not supported on this platform.
        """
        if not ctx.deps.tentacle:
            return "not supported"
        key = ctx.deps.session_key
        item = await ctx.deps.tentacle.feelers.todos.create_todo(key, title)
        return f"todo:{item.todo_id}" if item else "not supported on this platform"

    return toolset
