from __future__ import annotations

from dataclasses import dataclass
from typing import Never

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from octomate.telemetry import react_logfire


@dataclass
class ToolFailureCapability(AbstractCapability[None]):
    """Report a failed tool to the model instead of ending the turn.

    Pydantic AI ends a run when a tool raises anything but its own control-flow
    signals, so one broken call — a bad argument, a provider that blinked, a
    spell used out of order — costs the whole answer: the stream stops mid-way,
    whatever had already been drawn is abandoned, and the person is left with a
    trace id instead of a reply. The model is the one that can work around a
    failed call, and the one that can say what went wrong, so it is told.

    `ToolFailed` rather than `ModelRetry`: a call that failed for reasons the
    model did not cause is not a call to make again, and a retry spends a budget
    that ends the run when it runs out — which is the same lost turn, one attempt
    later. A failure it can read leaves it free to try another way or explain.
    Runs stay bounded by `UsageLimits.request_limit` (50 by default), not by how
    many times one tool may break.
    """

    async def on_tool_execute_error(
        self,
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        error: Exception,
    ) -> Never:
        # The model is told a sentence; the traceback is only ever seen here, so it
        # carries the exception itself and the ids to find the run it belongs to.
        react_logfire.exception(
            "tool {tool_name} failed; reported to the model",
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            run_id=ctx.run_id,
            _exc_info=error,
        )
        raise ToolFailed(f"`{call.tool_name}` failed: {error}") from error
