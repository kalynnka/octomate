"""Translate ACP prompts and run output without owning execution or transport.

Follows Pydantic AI Harness's ACP adapter for text, thinking and function tools,
and adds Octomate's segment output and non-streaming results. Segments use their
existing text representation; reply handles are omitted. Output-tool events are
not rendered: their validated answer arrives through the result events.

Create one translator per prompt and retain it across deferred resumes. Run starts
reset output tracking; tool IDs remain known across resumes. Other events produce
no updates here: routing, actions, plans and session lifecycle need their own
handling before this translator can serve a complete ACP channel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import acp
from acp import schema
from pydantic import JsonValue, TypeAdapter
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_core import to_json

from octomate.capabilities.harness.events import (
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    RunResultEvent,
    RunStartedEvent,
    WireEvent,
)
from octomate.schemas.segments import MessageSegment, ReplySegment, TextSegment

# Leave room for the JSON-RPC envelope in clients with a 64 KiB line buffer.
TEXT_CHUNK_BYTES = 48 * 1024
# A JSON preview is escaped again when embedded in its truncation marker.
RAW_PAYLOAD_BYTES = 16 * 1024

type AcpUpdate = (
    schema.AgentMessageChunk
    | schema.AgentThoughtChunk
    | schema.ToolCallStart
    | schema.ToolCallProgress
)

json_value_adapter = TypeAdapter(JsonValue)


def prompt_segments(request: schema.PromptRequest) -> list[MessageSegment]:
    """Translate the mandatory ACP prompt content into Octomate message segments.

    Text blocks retain their whitespace and order. Resource links use Pydantic AI
    Harness's presentation: the title or name followed by the URI, or the URI alone
    when neither label is present. A link is context, not a local file to open or a
    URL to fetch during translation.

    Image, audio and embedded-resource blocks require optional capabilities this
    adapter does not yet provide. Reject the whole request instead of dropping
    unsupported content. Empty and whitespace-only prompts are rejected before a
    caller records an inbound message or starts a run.
    """
    segments: list[MessageSegment] = []
    for block in request.prompt:
        match block:
            case schema.TextContentBlock(text=text):
                pass
            case schema.ResourceContentBlock():
                label = block.title or block.name
                text = f"{label} ({block.uri})" if label else block.uri
            case _:
                raise acp.RequestError.invalid_params(
                    {"reason": f"Unsupported prompt content: {block.type}"}
                )
        segments.append(TextSegment(data={"text": text}))
    if not any(str(segment).strip() for segment in segments):
        raise acp.RequestError.invalid_params({"reason": "Prompt carried no text"})
    return segments


def tool_payload(part: ToolCallPart | ToolReturnPart | RetryPromptPart) -> JsonValue:
    """Serialize tool data with an explicit oversized-data preview.

    Raw bytes use base64; Pydantic models retain their own serialization settings.
    The limit measures ASCII-escaped JSON, as used by the ACP SDK's transport.
    Unsupported Python values raise a serialization error rather than becoming reprs.
    Only the presentation is bounded; the original event and ledger stay intact.
    """
    match part:
        case ToolCallPart():
            value = part.args
        case RetryPromptPart():
            value = part.model_response()
        case ToolReturnPart():
            value = part.content
    payload = json_value_adapter.validate_json(to_json(value, bytes_mode="base64"))
    serialized = json.dumps(payload)
    if len(serialized) <= RAW_PAYLOAD_BYTES:
        return payload
    return {
        "truncated": True,
        "original_length": len(serialized),
        "preview": serialized[:RAW_PAYLOAD_BYTES],
    }


@dataclass
class AcpTranslator:
    """Present one prompt's run events as typed ACP session-update payloads.

    The caller supplies the session ID when sending each update. Text starts carry
    content too; part ends contain accumulated content and must not be replayed.
    Function-tool execution events, rather than streamed argument fragments, announce
    tools. A repeated call ID after approval is announced only once.
    """

    started_tools: set[str] = field(default_factory=set)  # IDs announced this prompt.
    answered: bool = False  # Whether the current model response has emitted an answer.

    def translate(self, event: WireEvent) -> list[AcpUpdate]:
        """Return zero or more updates, preserving stream order and tool-call IDs."""
        match event:
            case RunStartedEvent():
                self.answered = False
            case PartStartEvent(part=TextPart(content=text)):
                return self.text_updates(text)
            case PartDeltaEvent(delta=TextPartDelta(content_delta=text)):
                return self.text_updates(text)
            case PartStartEvent(part=ThinkingPart(content=text)):
                return self.text_updates(text, thought=True)
            case PartDeltaEvent(delta=ThinkingPartDelta(content_delta=text)):
                return self.text_updates(text or "", thought=True)
            case ResultTextDeltaEvent(delta=text):
                return self.text_updates(text)
            case ResultSegmentEvent(segment=ReplySegment()):
                return []
            case ResultSegmentEvent(segment=segment):
                return self.text_updates(str(segment))
            case FunctionToolCallEvent(part=part):
                if part.tool_call_id in self.started_tools:
                    return []
                update = acp.start_tool_call(
                    tool_call_id=part.tool_call_id,
                    title=part.tool_name,
                    status="in_progress",
                    raw_input=tool_payload(part),
                )
                self.started_tools.add(part.tool_call_id)
                return [update]
            case FunctionToolResultEvent(part=part):
                # Commentary before a tool is not the next model response's answer.
                self.answered = False
                failed = isinstance(part, RetryPromptPart) or part.outcome != "success"
                return [
                    acp.update_tool_call(
                        tool_call_id=part.tool_call_id,
                        status="failed" if failed else "completed",
                        raw_output=tool_payload(part),
                    )
                ]
            case RunResultEvent(output=output) if not self.answered:
                if isinstance(output, str):
                    return self.text_updates(output)
                return [
                    update
                    for segment in output or []
                    if not isinstance(segment, ReplySegment)
                    for update in self.text_updates(str(segment))
                ]
        return []

    def text_updates(self, text: str, *, thought: bool = False) -> list[AcpUpdate]:
        """Split text by its JSON-escaped size, without losing Unicode or whitespace."""
        updates: list[AcpUpdate] = []
        chunk: list[str] = []
        size = 0
        for char in text:
            escaped_size = len(json.dumps(char)) - 2
            if chunk and size + escaped_size > TEXT_CHUNK_BYTES:
                updates.append(
                    acp.update_agent_thought_text("".join(chunk))
                    if thought
                    else acp.update_agent_message_text("".join(chunk))
                )
                chunk = []
                size = 0
            chunk.append(char)
            size += escaped_size
        if chunk:
            updates.append(
                acp.update_agent_thought_text("".join(chunk))
                if thought
                else acp.update_agent_message_text("".join(chunk))
            )
        if text and not thought:
            self.answered = True
        return updates
