from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
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

from octomate.capabilities.events import (
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
from octomate.tentacles.channel.feelers.output import (
    IMMessageID,
    JsonValue,
    TextStreamBatcher,
    TimelineFeeler,
    TimelineState,
    format_fields,
    humanize_tool_name,
    render_todo_lines,
    should_skip_plan_tool,
    truncate_task_detail,
)
from octomate.tentacles.channel.lark.chromo import LARK_STREAM_ELEMENT_ID, LarkChromo
from octomate.tentacles.channel.lark.feelers import cards
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage, LarkStreamCard

if TYPE_CHECKING:
    from octomate.managers.deferred import DeferredActionManager
    from octomate.tentacles.channel.feelers.deferred import (
        ApprovalFeeler,
        QuestionFeeler,
    )
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)
OutputT = TypeVar(
    "OutputT", bound=JsonValue | Sequence[MessageSegment] | DeferredToolRequests
)

THINKING_HEADER = "Thinking"

# Each live thinking re-render is one card-patch API call, so updates are
# paced rather than sent per delta.
THINKING_FLUSH_INTERVAL = 1.0


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

    async def present(
        self,
        address: ChannelAddress,
        markdown: str,
    ) -> IMMessageID | None:
        chat_id = address.chat_id or address.user_id
        reply_to = address.thread_id if address.thread_id.startswith("om_") else None
        return await self.ink.send_message(
            chat_id,
            address.chat_type,
            self.chromo.outbound_markdown(markdown),
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
    deferred_actions: DeferredActionManager

    message_id: IMMessageID | None = None
    answer_card: LarkStreamCard | None = None
    answer_message_id: IMMessageID | None = None
    answer_sequence: int = 0
    saw_answer: bool = False

    thinking_card_id: str | None = None
    thinking_text: str = ""
    thinking_emitted_at: float = 0.0
    thinking_started_at: float = 0.0
    tool_cards: dict[str, tuple[str | None, str, str]] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    todos: dict[str, Todo] = field(default_factory=dict)
    todo_card_id: str | None = None

    async def post(self, card: JsonObject) -> str | None:
        return await self.ink.send_message(
            self.chat_id,
            self.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                )
            ],
            self.reply_to,
            reply_in_thread=self.reply_in_thread,
        )

    async def rotate(self) -> None:
        """The mid-run notice streamed into the answer card — finalize it so
        the next answer text (and todo checklist) opens a fresh card below the
        new activity instead of updating one stranded above it."""
        # Flush first: a short notice may still sit in the batcher without
        # having created the card yet.
        for update in self.answer_batcher.finish_all():
            await self.push_answer(update.full_text)
        if self.answer_card is None:
            return
        self.answer_sequence += 1
        await self.ink.finish_stream_card(
            self.answer_card, sequence=self.answer_sequence
        )
        self.answer_card = None
        self.answer_message_id = None
        self.answer_sequence = 0
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
        self.thinking_started_at = time.monotonic()
        self.thinking_card_id = await self.post(
            cards.card_v2([cards.markdown(f"**{THINKING_HEADER}…**")])
        )
        self.thinking_emitted_at = self.thinking_started_at

    async def thinking_delta(self, text: str) -> None:
        if not text:
            return
        if self.thinking_card_id is None:
            await self.thinking_start()
        self.thinking_text += text
        now = time.monotonic()
        if (
            self.thinking_card_id is not None
            and now - self.thinking_emitted_at >= THINKING_FLUSH_INTERVAL
        ):
            self.thinking_emitted_at = now
            live = cards.card_v2(
                [cards.markdown(f"**{THINKING_HEADER}…**\n\n{self.thinking_text}")]
            )
            await self.ink.patch_card(
                self.thinking_card_id,
                json.dumps(live, ensure_ascii=False, separators=(",", ":")),
            )

    async def fold_thinking(self) -> None:
        if self.thinking_card_id is None:
            return
        card_id = self.thinking_card_id
        body = self.thinking_text.strip() or "_No detail_"
        self.thinking_card_id = None
        self.thinking_text = ""
        elapsed = max(1, round(time.monotonic() - self.thinking_started_at))
        folded = cards.card_v2(
            [
                cards.collapsible_panel(
                    f"Thought for {elapsed}s", [cards.markdown(body)]
                )
            ]
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

    async def answer_delta(self, text: str | None) -> None:
        if not text:
            return
        await self.fold_thinking()
        self.noticed = True
        for update in self.answer_batcher.push_text(text):
            await self.push_answer(update.full_text)

    async def answer_segment(self, segment: MessageSegment) -> None:
        match segment:
            case AtSegment():
                await self.answer_delta(f"<at id={segment.data.user_id}></at>")
            case ImageSegment():
                await self.fold_thinking()
                image_key = await self.ink.upload_media(
                    segment.data.path.read_bytes()
                )
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
        for update in self.answer_batcher.finish_all():
            await self.push_answer(update.full_text)
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
        deferred_actions: DeferredActionManager,
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        self.stream_config = stream_config
        self.ask_questions = ask_questions
        self.approvals = approvals
        self.deferred_actions = deferred_actions

    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncIterator[LarkRunStateCards]:
        reply_to = address.thread_id if address.thread_id.startswith("om_") else None
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
            deferred_actions=self.deferred_actions,
        )
        try:
            yield state
        finally:
            # consume() already fed any non-streamed final output via answer_delta,
            # so the result-fallback arg is None here.
            await state.finish(None)
            state.message_id = state.answer_message_id
