from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    RetryPromptPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.events import (
    SubagentActivity,
    SubagentActivityStatus,
    TodoDeletedEvent,
    TodoEvent,
)
from octomate.config import ChannelStreamConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    AtSegment,
    CardSegment,
    ImageSegment,
    MessageSegment,
)
from octomate.schemas.todos import Todo
from octomate.telemetry import lark_logfire
from octomate.tentacles.channels.feelers.output import (
    IMMessageID,
    JsonValue,
    MarkdownChunker,
    StreamFlusher,
    SubagentTimelineState,
    TextStreamBatcher,
    TimelineFeeler,
    TimelineState,
    format_fields,
    humanize_tool_name,
    render_todo_lines,
    should_skip_plan_tool,
    truncate_task_detail,
)
from octomate.tentacles.channels.lark.chromo import LARK_STREAM_ELEMENT_ID, LarkChromo
from octomate.tentacles.channels.lark.feelers import cards
from octomate.tentacles.channels.lark.ink import LarkInk
from octomate.tentacles.channels.lark.schema import LarkOutboundMessage, LarkStreamCard

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager
    from octomate.tentacles.channels.feelers.deferred import (
        ApprovalFeeler,
        QuestionFeeler,
    )
    from octomate.tentacles.channels.feelers.oauth import OAuthFeeler
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)
OutputT = TypeVar(
    "OutputT", bound=JsonValue | Sequence[MessageSegment] | DeferredToolRequests
)

THINKING_HEADER = "Thinking"
CARD_RENDER_FALLBACK_HINT = (
    "I couldn't render this as a Lark card, so here's the raw message:"
)


def card_render_fallback_text(raw_message: str) -> str:
    return f"{CARD_RENDER_FALLBACK_HINT}\n\n{raw_message}"


def format_tool_arguments(_tool_name: str, args: dict[str, Any]) -> str:
    if not args:
        return "_No arguments_"
    return format_fields(args, bold="**")


def format_tool_result(part: ToolReturnPart | RetryPromptPart) -> str:
    if isinstance(part, RetryPromptPart):
        return truncate_task_detail(part.model_response())
    value = part.model_response_object()
    if not value:
        return "_No result_"
    return format_fields(value, bold="**")


class LarkMarkdownFeeler:
    def __init__(self, *, ink: LarkInk, chromo: LarkChromo) -> None:
        self.ink = ink
        self.chromo = chromo

    @lark_logfire.instrument("lark.markdown.present", extract_args=False)
    async def present(
        self,
        address: ChannelAddress,
        markdown: str,
    ) -> IMMessageID | None:
        if not markdown:
            return None
        chat_id = address.chat_id or address.user_id
        reply_to = (
            address.channel_thread_id
            if address.channel_thread_id and address.channel_thread_id.startswith("om_")
            else None
        )
        message_id = await self.ink.send_message(
            chat_id,
            address.chat_type,
            self.chromo.outbound_markdown(markdown),
            reply_to,
            reply_in_thread=reply_to is not None,
        )
        if message_id is not None:
            return message_id
        return await self.ink.send_text_message(
            chat_id,
            address.chat_type,
            card_render_fallback_text(markdown),
            reply_to,
            reply_in_thread=reply_to is not None,
        )


def answer_stream_card_data() -> str:
    payload = {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "summary": {"content": ""},
            "streaming_config": {
                "print_frequency_ms": {
                    "default": 20,
                    "android": 20,
                    "ios": 20,
                    "pc": 20,
                },
                "print_step": {"default": 12, "android": 12, "ios": 12, "pc": 12},
                "print_strategy": "fast",
            },
        },
        "body": {"elements": [cards.markdown("", element_id=LARK_STREAM_ELEMENT_ID)]},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass
class LarkRunStateCards(TimelineState):
    """The per-run cards for one agent run. Each thinking block and each tool call
    is its own card — sent on start, then patched into a folded collapsible panel
    once it finishes — and the answer streams into its own card."""

    ink: LarkInk
    address: ChannelAddress
    chat_id: str
    chat_type: str
    reply_to: str | None
    reply_in_thread: bool
    answer_batcher: TextStreamBatcher
    ask_questions: QuestionFeeler
    approvals: ApprovalFeeler
    oauth: OAuthFeeler
    deferred_actions: DeferredActionManager

    message_id: IMMessageID | None = None
    answer_card: LarkStreamCard | None = None
    answer_message_id: IMMessageID | None = None
    answer_sequence: int = 0
    saw_answer: bool = False
    answer_sent_len: int = 0
    answer_flusher: StreamFlusher = field(init=False)

    thinking_card_id: str | None = None
    thinking_text: str = ""
    thinking_patched_len: int = 0
    thinking_started_at: float = 0.0
    thinking_flusher: StreamFlusher = field(init=False)
    tool_cards: dict[str, tuple[str | None, str, str]] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    todos: dict[str, Todo] = field(default_factory=dict)
    todo_card_id: str | None = None

    def __post_init__(self) -> None:
        self.thinking_flusher = StreamFlusher(self.flush_thinking)
        self.answer_flusher = StreamFlusher(self.flush_answer)

    @asynccontextmanager
    async def open_subagent(
        self,
        activity: SubagentActivity,
    ) -> AsyncGenerator[LarkSubagentTimelineState]:
        state = LarkSubagentTimelineState(
            ink=self.ink,
            activity=activity,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
            reply_to=self.reply_to,
            reply_in_thread=self.reply_in_thread,
        )
        await state.start()
        yield state

    async def post(self, card: JsonObject) -> str | None:
        raw_card = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        message_id = await self.ink.send_message(
            self.chat_id,
            self.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=raw_card,
                )
            ],
            self.reply_to,
            reply_in_thread=self.reply_in_thread,
        )
        if message_id is not None:
            return message_id
        return await self.ink.send_text_message(
            self.chat_id,
            self.chat_type,
            card_render_fallback_text(raw_card),
            self.reply_to,
            reply_in_thread=self.reply_in_thread,
        )

    async def rotate(self) -> None:
        """The mid-run notice streamed into the answer card — finalize it so
        the next answer text (and todo checklist) opens a fresh card below the
        new activity instead of updating one stranded above it."""
        # Drain first: a short notice may still sit in the batcher without having
        # created the card yet.
        await self.drain_answer()
        if self.answer_card is None:
            return
        self.answer_sequence += 1
        await self.ink.finish_stream_card(
            self.answer_card, sequence=self.answer_sequence
        )
        self.answer_card = None
        self.answer_message_id = None
        self.answer_sequence = 0
        self.answer_sent_len = 0
        self.todo_card_id = None
        self.answer_batcher = TextStreamBatcher(
            flush_interval=self.answer_batcher.flush_interval,
            min_chars=self.answer_batcher.min_chars,
            max_chars=self.answer_batcher.max_chars,
            fold_threshold=self.answer_batcher.fold_threshold,
        )

    async def thinking_start(self) -> None:
        await self.begin_entry()
        await self.fold_thinking()
        self.thinking_text = ""
        self.thinking_patched_len = 0
        self.thinking_started_at = time.monotonic()
        self.thinking_card_id = await self.post(
            cards.card_v2([cards.markdown(f"**{THINKING_HEADER}…**")])
        )

    async def thinking_delta(self, text: str) -> None:
        if not text:
            return
        if self.thinking_card_id is None:
            await self.thinking_start()
        self.thinking_text += text
        self.thinking_flusher.signal()

    async def flush_thinking(self) -> None:
        """Re-render the live thinking card off the drive loop: each patch replaces
        the whole card with the latest text (`fold_thinking` then closes the flusher
        and re-renders as the folded panel)."""
        if (
            self.thinking_card_id is None
            or len(self.thinking_text) <= self.thinking_patched_len
        ):
            return
        card_id = self.thinking_card_id
        text = self.thinking_text
        self.thinking_patched_len = len(text)
        live = cards.card_v2([cards.markdown(f"**{THINKING_HEADER}…**\n\n{text}")])
        await self.ink.patch_card(
            card_id, json.dumps(live, ensure_ascii=False, separators=(",", ":"))
        )

    async def fold_thinking(self) -> None:
        if self.thinking_card_id is None:
            return
        await self.thinking_flusher.drain()
        card_id = self.thinking_card_id
        body = self.thinking_text.strip() or "_No detail_"
        self.thinking_card_id = None
        self.thinking_text = ""
        self.thinking_patched_len = 0
        elapsed = max(1, round(time.monotonic() - self.thinking_started_at))
        folded = cards.card_v2(
            [cards.collapsible_panel(f"Thought for {elapsed}s", [cards.markdown(body)])]
        )
        await self.ink.patch_card(
            card_id, json.dumps(folded, ensure_ascii=False, separators=(",", ":"))
        )

    async def tool_start(
        self, event: FunctionToolCallEvent | OutputToolCallEvent
    ) -> None:
        await self.begin_entry()
        await self.fold_thinking()
        tool = event.part
        if should_skip_plan_tool(tool.tool_name):
            if tool.tool_call_id:
                self.skipped.add(tool.tool_call_id)
            return
        title = humanize_tool_name(tool.tool_name)
        if isinstance(event, OutputToolCallEvent):
            title = f"Output: {title}"
        args = tool.args_as_dict()
        args_text = format_tool_arguments(tool.tool_name, args) if args else ""
        body = f"**{title}**\n\n**Tool**\n`{tool.tool_name}`"
        if args_text:
            body += f"\n\n**Arguments**\n{args_text}"
        message_id = await self.post(cards.card_v2([cards.markdown(body)]))
        slot = tool.tool_call_id or f"tool-{len(self.tool_cards)}"
        self.tool_cards[slot] = (message_id, title, args_text)

    async def tool_end(
        self, event: FunctionToolResultEvent | OutputToolResultEvent
    ) -> None:
        part = event.part
        tool_name = part.tool_name or "output"
        tool_call_id = getattr(part, "tool_call_id", None)
        if should_skip_plan_tool(tool_name) or (
            tool_call_id is not None and tool_call_id in self.skipped
        ):
            if tool_call_id:
                self.skipped.discard(tool_call_id)
            return
        entry = self.tool_cards.pop(tool_call_id or "", None)
        message_id, title, args_text = (
            entry if entry is not None else (None, humanize_tool_name(tool_name), "")
        )
        error = isinstance(part, RetryPromptPart)
        sections: list[str] = [f"**Tool**\n`{tool_name}`"]
        if args_text:
            sections.append(f"**Arguments**\n{args_text}")
        sections.append(f"**Result**\n{format_tool_result(part)}")
        folded = cards.card_v2(
            [
                cards.collapsible_panel(
                    f"{title} (failed)" if error else title,
                    [cards.markdown("\n\n".join(sections))],
                )
            ]
        )
        payload = json.dumps(folded, ensure_ascii=False, separators=(",", ":"))
        if message_id is not None:
            await self.ink.patch_card(message_id, payload)
        else:
            await self.post(folded)

    async def answer_start(self) -> None:
        # Open the answer card when the text part starts rather than on its first
        # flush: creating and sending it is ~0.9s of round trips, and leaving it to
        # the first delta puts all of it between the last token and the first
        # visible character.
        await self.ensure_answer_card()

    async def answer_delta(self, text: str | None) -> None:
        if not text:
            return
        self.noticed = True
        self.answer_batcher.push_text(text)
        # Signal before folding, not after: the flusher pushes the card from its own
        # task, so the fold's patch and the answer's update are two round trips at
        # once instead of one behind the other. The answer card lands under a
        # thinking card that is still open and collapses a moment later — the
        # trade for showing the answer a round trip sooner.
        self.answer_flusher.signal()
        await self.fold_thinking()

    async def flush_answer(self) -> None:
        """Push the answer card off the drive loop: each cardkit update is one ~2s
        round-trip, so signals coalesce to one update of the latest text while the
        drive loop keeps draining tokens — no backpressure on generation, and the
        card stays caught up instead of trailing seconds behind."""
        full_text = self.answer_batcher.full_text()
        if len(full_text) <= self.answer_sent_len:
            return
        self.answer_sent_len = len(full_text)
        await self.push_answer(full_text)

    async def drain_answer(self) -> None:
        """Stop the flusher and push the final text, so the settled card holds the
        complete answer (a mid-run notice that never opened a card lands here too)."""
        await self.answer_flusher.drain()
        await self.flush_answer()

    async def answer_segment(self, segment: MessageSegment) -> None:
        match segment:
            case AtSegment():
                await self.answer_delta(f"<at id={segment.data.user_id}></at>")
            case ImageSegment():
                await self.fold_thinking()
                image_key = await self.ink.upload_media(segment.data.path.read_bytes())
                if image_key is None:
                    raise RuntimeError("failed to upload Lark image")
                await self.ink.send_message(
                    self.chat_id,
                    self.chat_type,
                    [
                        LarkOutboundMessage(
                            msg_type="image",
                            content=json.dumps({"image_key": image_key}),
                        )
                    ],
                    self.reply_to,
                    reply_in_thread=self.reply_in_thread,
                )
            case CardSegment():
                await self.fold_thinking()
                await self.post(segment.data.payload)
            case _:
                await self.answer_delta(str(segment))

    async def todo(self, event: TodoEvent) -> None:
        await self.begin_entry()
        if isinstance(event, TodoDeletedEvent):
            self.todos.pop(event.todo.ref, None)
        else:
            self.todos[event.todo.ref] = event.todo
        if not self.todos and self.todo_card_id is None:
            return
        body = "**Tasks**\n" + "\n".join(render_todo_lines(self.todos.values()))
        card = cards.card_v2([cards.markdown(body)])
        if self.todo_card_id is None:
            self.todo_card_id = await self.post(card)
        else:
            await self.ink.patch_card(
                self.todo_card_id,
                json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            )

    async def ensure_answer_card(self) -> LarkStreamCard:
        if self.answer_card is None:
            self.answer_card = await self.ink.create_stream_card(
                answer_stream_card_data(), element_id=LARK_STREAM_ELEMENT_ID
            )
            self.answer_message_id = await self.ink.send_stream_card(
                self.chat_id,
                self.chat_type,
                self.answer_card,
                reply_to=self.reply_to,
                reply_in_thread=self.reply_in_thread,
            )
            if self.answer_message_id is None:
                raise RuntimeError("failed to send Lark answer card")
        return self.answer_card

    async def push_answer(self, full_text: str) -> None:
        card = await self.ensure_answer_card()
        self.answer_sequence += 1
        self.saw_answer = True
        if not await self.ink.update_stream_card(
            card,
            content=full_text,
            sequence=self.answer_sequence,
        ):
            raise RuntimeError("failed to update Lark answer card")

    async def finish(self, result_event: AgentRunResultEvent[OutputT] | None) -> None:
        await self.fold_thinking()
        await self.drain_answer()
        if (
            not self.saw_answer
            and result_event is not None
            and isinstance(result_event.result.output, str)
            and result_event.result.output
        ):
            await self.push_answer(result_event.result.output)
        if self.answer_card is not None:
            self.answer_sequence += 1
            await self.ink.finish_stream_card(
                self.answer_card, sequence=self.answer_sequence
            )


class LarkTimelineFeeler(TimelineFeeler):
    """Renders a run as Lark cards (a card per thinking block / tool call + a
    streaming answer card), driven event-by-event by `ChannelTentacle.consume`.

    Stateless; `open(address)` builds a fresh per-run `LarkRunStateCards` machine that
    `drive_timeline` renders each event onto."""

    def __init__(
        self,
        *,
        ink: LarkInk,
        chromo: LarkChromo,
        stream_config: ChannelStreamConfig,
        ask_questions: QuestionFeeler,
        approvals: ApprovalFeeler,
        oauth: OAuthFeeler,
        deferred_actions: DeferredActionManager,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.stream_config = stream_config
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.oauth = oauth
        self.deferred_actions = deferred_actions

    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncGenerator[LarkRunStateCards]:
        reply_to = (
            address.channel_thread_id
            if address.channel_thread_id and address.channel_thread_id.startswith("om_")
            else None
        )
        state = LarkRunStateCards(
            ink=self.ink,
            address=address,
            chat_id=address.chat_id or address.user_id,
            chat_type=address.chat_type,
            reply_to=reply_to,
            reply_in_thread=reply_to is not None,
            answer_batcher=TextStreamBatcher(
                flush_interval=self.stream_config.flush_interval,
                min_chars=self.stream_config.min_chars,
                max_chars=self.stream_config.max_chars,
                fold_threshold=self.stream_config.fold_threshold,
            ),
            ask_questions=self.ask_questions,
            approvals=self.approvals,
            oauth=self.oauth,
            deferred_actions=self.deferred_actions,
        )
        try:
            yield state
        except asyncio.CancelledError:
            await state.settle_subagents("cancelled")
            raise
        finally:
            await state.settle_subagents("failed")
            # consume() already fed any non-streamed final output via answer_delta,
            # so the result-fallback arg is None here.
            await state.finish(None)
            state.message_id = state.answer_message_id


@dataclass
class LarkSubagentTimelineState(SubagentTimelineState):
    ink: LarkInk
    activity: SubagentActivity
    chat_id: str
    chat_type: str
    reply_to: str | None
    reply_in_thread: bool

    card_id: str | None = None
    response: str = ""
    status: SubagentActivityStatus | None = None

    def card(self, *, expanded: bool) -> JsonValue:
        template = "blue"
        title = f"Starting {self.activity.name}…"
        if self.status == "completed":
            template = "green"
            title = "Completed"
        elif self.status is not None:
            template = "red"
            title = {
                "failed": "Failed",
                "timed_out": "Timed out",
                "cancelled": "Cancelled",
            }[self.status]
        elements: list[JsonValue] = [cards.markdown(f"**{title}**")]
        chunks = MarkdownChunker().chunk(self.response) if self.response else []
        for index, chunk in enumerate(chunks):
            title = "Response" if index == 0 else f"Response ({index + 1})"
            elements.append(
                cards.collapsible_panel(
                    title,
                    [cards.markdown(chunk)],
                    expanded=expanded,
                )
            )
        return cards.card_v2(
            elements,
            header=cards.header(
                f"Subagent · {self.activity.name}",
                template=template,
            ),
        )

    async def start(self) -> None:
        payload = json.dumps(
            self.card(expanded=True), ensure_ascii=False, separators=(",", ":")
        )
        self.card_id = await self.ink.send_message(
            self.chat_id,
            self.chat_type,
            [LarkOutboundMessage(msg_type="interactive", content=payload)],
            self.reply_to,
            reply_in_thread=self.reply_in_thread,
        )
        if self.card_id is None:
            self.card_id = await self.ink.send_text_message(
                self.chat_id,
                self.chat_type,
                card_render_fallback_text(payload),
                self.reply_to,
                reply_in_thread=self.reply_in_thread,
            )

    async def render(self, *, expanded: bool) -> None:
        if self.card_id is None:
            raise RuntimeError("subagent card was not created")
        payload = json.dumps(
            self.card(expanded=expanded),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self.ink.patch_card(self.card_id, payload)

    async def append_response(self, delta: str) -> None:
        if self.status is not None or not delta:
            return
        self.response += delta

    async def settle(
        self,
        status: SubagentActivityStatus,
        detail: str | None = None,
    ) -> None:
        if self.status is not None:
            return
        if detail:
            self.response = f"{self.response}\n\n{detail}" if self.response else detail
        self.status = status
        await self.render(expanded=False)
