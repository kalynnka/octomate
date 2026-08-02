"""Vercel UI event stream that also speaks the octomate `StreamEvents`.

The stock `VercelAIEventStream` silently drops events it does not know
(`handle_event`'s catch-all). The react stream is normalized, so text replies
arrive as native Pydantic events, segment replies arrive as `ResultSegmentEvent`,
and capabilities add display/action events — this subclass renders the
Octomate-specific ones onto the Vercel protocol:

- text replies pass through to the stock Vercel handler — but the octomate-managed
  text part (a mid-run notice / todo note / segment) is closed first, so the two
  never interleave into one unbounded part (which crashes the dev UI),
- segment replies stream as one custom text part,
- a send capability's ``MessageSentEvent`` streams as its own closed text block,
- todo events render as a markdown text note (the stock dev UI does not render
  custom ``data-*`` parts, so they would otherwise be invisible),
- a pending OAuth authorization renders as its link and code in text,
- a suspended run's deferred-action batch renders as a markdown text summary,
- everything else (thinking, tool calls, the run result) passes to the stock
  handlers.

Deferred *tool requests* (the Vercel client-side-call path) are noop'd in this
dev channel — there is no resolver/suspender, so the run simply ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartStartEvent,
    TextPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.vercel_ai import VercelAIEventStream
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    TextDeltaChunk,
    TextEndChunk,
    TextStartChunk,
)

from octomate.capabilities.gateway import COMMISSION_TOOL_NAME, WHISPER_TOOL_NAME
from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    MessageSentEvent,
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
    ResultSegmentEvent,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoStatusChangedEvent,
    TodoUpdatedEvent,
)
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.tentacles.agent.inkling.base import InklingOutput


@dataclass
class VercelEventStream(VercelAIEventStream[None, InklingOutput]):
    reply_id: str | None = None

    async def handle_event(
        self,
        event: NativeEvent | ReactStreamEvent[InklingOutput] | BaseChunk,
    ) -> AsyncIterator[BaseChunk]:
        match event:
            case FunctionToolCallEvent() | FunctionToolResultEvent() if (
                event.part.tool_name in {COMMISSION_TOOL_NAME, WHISPER_TOOL_NAME}
            ):
                return
            case BaseChunk():
                async for chunk in self.close_reply():
                    yield chunk
                yield event
            case PartStartEvent(part=TextPart()):
                # The native reply's text part is opening — close any mid-run notice
                # block first so the two text parts don't interleave (an unbounded
                # part crashes the dev UI).
                async for chunk in self.close_reply():
                    yield chunk
                async for chunk in super().handle_event(event):
                    yield chunk
            case ResultSegmentEvent():
                # Completed segments join the reply text as separate blocks.
                text = str(event.segment)
                if self.reply_id is not None:
                    text = f"\n\n{text}"
                async for chunk in self.reply_delta(text):
                    yield chunk
            case FinalResult():
                async for chunk in self.close_reply():
                    yield chunk
            case (
                TodoCreatedEvent()
                | TodoUpdatedEvent()
                | TodoStatusChangedEvent()
                | TodoCompletedEvent()
                | TodoDeletedEvent()
            ):
                todo = event.todo
                note = (
                    f"\n\n*[{event.event_kind}]* `{todo.ref}` {todo.content} "
                    f"— _{todo.status}_"
                )
                async for chunk in self.reply_delta(note):
                    yield chunk
            case MessageSentEvent():
                # Emit-only send: render the delivered segments as reply content,
                # the same way streamed segment replies are rendered.
                text = "\n\n".join(str(segment) for segment in event.segments)
                if self.reply_id is not None:
                    text = f"\n\n{text}"
                async for chunk in self.reply_delta(text):
                    yield chunk
            case OAuthAuthorizationEvent():
                # A pending authorization the user must complete; the dev UI has no
                # card to put a button on, so it renders it as text — with the code
                # only when there is one to type.
                note = f"\n\n**Connect {event.label}**: {event.authorization_uri}"
                if isinstance(event, OAuthDeviceAuthorizationEvent):
                    note += f" — code `{event.user_code}`"
                async for chunk in self.reply_delta(note):
                    yield chunk
            case ActionBatchEvent():
                lines = ["\n\n**Pending input** _(noop in dev UI)_"]
                lines += [f"- ❓ {q.args['question']}" for q in event.questions]
                lines += [f"- ✅ {a.args.title}" for a in event.approvals]
                async for chunk in self.reply_delta("\n".join(lines)):
                    yield chunk
            case _:
                async for chunk in super().handle_event(event):
                    yield chunk

    async def close_reply(self) -> AsyncIterator[BaseChunk]:
        """End the current octomate-managed text part, if one is open."""
        if self.reply_id is not None:
            yield TextEndChunk(id=self.reply_id)
            self.reply_id = None

    async def reply_delta(self, text: str) -> AsyncIterator[BaseChunk]:
        if not text:
            return
        async for chunk in self._turn_to("response"):
            yield chunk
        if self.reply_id is None:
            self.reply_id = self.new_message_id()
            yield TextStartChunk(id=self.reply_id)
        yield TextDeltaChunk(id=self.reply_id, delta=text)
