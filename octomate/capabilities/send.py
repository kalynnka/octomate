"""Send capability: one tool to deliver content to the run's conversation mid-run.

Emit-only. The agent decides *what* to send (the `OutputSegment` vocabulary —
text, markdown, image, file, card); the system decides *where* implicitly: the
tool emits a `MessageSentEvent` onto the run's event stream, and whichever consumer
is rendering this run (the channel timeline, or the web event stream) renders the
segments into the current conversation — the same surface that renders the reply.
The tool touches no channel, holds no registry, and resolves no conversation: there
is nothing to address, because the stream is already bound to one conversation.

Follows the `TodoCapability` pattern: the tool stashes the `MessageSentEvent` on
`ToolReturn.metadata` and `wrap_run_event_stream` forwards it onto the stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.capabilities.events import MessageSentEvent
from octomate.schemas.segments import OutputSegment

SEND_TOOL_NAME = "send_message"

SEND_INSTRUCTION = """\
## Sending content mid-run

You have a `send_message` tool to deliver content to this conversation *before*
your turn ends — progress updates, an intermediate result, or an image/file you
produced along the way. It always goes to the current conversation; you never
choose or name a channel.

- Use it for content the user should see while you keep working.
- Anything you send this way is already delivered. Your final reply continues from
  there — summarize or extend it, never restate what you already sent.
- If everything worth saying was already sent, return a minimal closing reply (a
  short wrap-up) rather than re-sending the content.
"""


def build_send_toolset() -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset(id="send")

    @toolset.tool(name=SEND_TOOL_NAME)
    async def send_message(
        ctx: RunContext[Any], segments: list[OutputSegment]
    ) -> ToolReturn[str]:
        """Send messages to the current conversation immediately, without ending
        your turn — progress updates, an intermediate result, an image or a file.
        Anything you send here is already delivered: do NOT repeat it in your final
        reply."""
        return ToolReturn(
            return_value="sent",
            metadata=[MessageSentEvent(segments=segments)],
        )

    return toolset


@dataclass
class SendCapability(AbstractCapability[Any]):
    """Adds the `send_message` tool + its instruction, and forwards the
    `MessageSentEvent` each call produces onto the run's event stream."""

    toolset: FunctionToolset[Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.toolset = build_send_toolset()

    def get_toolset(self) -> AbstractToolset[Any] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[Any] | None:
        return SEND_INSTRUCTION

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[Any],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        async for event in stream:
            yield event
            if (
                isinstance(event, FunctionToolResultEvent)
                and isinstance(event.part, ToolReturnPart)
                and event.part.tool_name == SEND_TOOL_NAME
                and isinstance(event.part.metadata, list)
            ):
                for sent_event in event.part.metadata:
                    # The tool stashed the MessageSentEvent (not an AgentStreamEvent)
                    # in metadata; inject it on the stream. One dynamic-boundary cast —
                    # pydantic-ai types the stream as AgentStreamEvent, consumers match
                    # the concrete octomate event type.
                    yield cast(AgentStreamEvent, sent_event)
