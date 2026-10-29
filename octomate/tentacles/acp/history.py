"""The conversation visible through ACP, folded into a driven run's messages.

ACP exposes presentation content, not native model turns. Each prompt becomes
one request and its output one response, labelled with `source=acp`. Original
content and updates are retained in metadata; model names, usage and native turn
IDs are not inferred. Reported context snapshots are independent of billing
usage. Tool titles are presentation names, not executable tools.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, replace

from acp import schema
from pydantic_ai.messages import (
    AgentStreamEvent,
    BinaryContent,
    FilePart,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from octomate.tentacles.acp.adapter import json_value_adapter
from octomate.types.json import JsonValue

type ContentBlock = (
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.AudioContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock
)


def content_value(block: ContentBlock) -> str | BinaryContent:
    """Keep media inline and resource links inert; never read a path or URL."""
    match block:
        case schema.TextContentBlock():
            return block.text
        case schema.ResourceContentBlock():
            label = block.title or block.name
            return f"{label} ({block.uri})" if label else block.uri
        case schema.ImageContentBlock() | schema.AudioContentBlock():
            return BinaryContent(
                data=base64.b64decode(block.data, validate=True),
                media_type=block.mime_type,
            )
        case schema.EmbeddedResourceContentBlock(
            resource=schema.TextResourceContents()
        ):
            return block.resource.text
        case schema.EmbeddedResourceContentBlock():
            return BinaryContent(
                data=base64.b64decode(block.resource.blob, validate=True),
                media_type=block.resource.mime_type or "application/octet-stream",
            )


@dataclass
class AcpHistory:
    """Accumulate one prompt in wire order, producing the same live and stored content.

    A changed message ID or content kind ends a text part. Tool updates replace
    supplied fields, retaining omitted fields. Raw updates also preserve plans,
    settings, diffs, terminals and provider metadata without inventing model parts.
    """

    prompt: schema.PromptRequest
    request: ModelRequest = field(init=False)
    response: ModelResponse = field(init=False)
    parts: list[ModelResponsePart] = field(default_factory=list)
    updates: list[JsonValue] = field(default_factory=list)
    tools: dict[str, schema.ToolCall] = field(default_factory=dict)
    calls: dict[str, NativeToolCallPart] = field(default_factory=dict)
    returns: dict[str, NativeToolReturnPart] = field(default_factory=dict)
    streaming: int | None = None  # The response part currently receiving chunks.
    message_id: str | None = None  # ACP's optional boundary, not a native turn ID.
    context_window: int | None = None  # Last reported context capacity.
    context_used: int | None = None  # Context occupancy, not billed input.

    def __post_init__(self) -> None:
        self.request = ModelRequest(
            parts=[
                UserPromptPart(content=[content_value(b) for b in self.prompt.prompt])
            ],
            metadata={
                "source": "acp",
                "prompt": json_value_adapter.validate_json(
                    self.prompt.model_dump_json(by_alias=True, exclude_none=True)
                ),
            },
        )
        self.response = ModelResponse(
            parts=self.parts,
            provider_name="acp",
            metadata={"source": "acp", "updates": self.updates},
        )

    @property
    def messages(self) -> list[ModelMessage]:
        return [self.request, self.response] if self.updates else [self.request]

    @property
    def answer(self) -> str:
        """The visible reply for the ledger; media stays intact in model history."""
        return "\n\n".join(
            part.content
            if isinstance(part, TextPart)
            else f"[{part.content.media_type}]"
            for part in self.parts
            if isinstance(part, TextPart | FilePart)
        )

    def end_part(self) -> list[AgentStreamEvent]:
        if self.streaming is None:
            return []
        index, self.streaming = self.streaming, None
        return [PartEndEvent(index=index, part=replace(self.parts[index]))]

    def feed(self, notification: schema.SessionNotification) -> list[AgentStreamEvent]:
        """Fold a validated update and return its live events in arrival order."""
        update = notification.update
        self.updates.append(
            json_value_adapter.validate_json(
                update.model_dump_json(by_alias=True, exclude_none=True)
            )
        )
        if isinstance(update, schema.UsageUpdate):
            self.context_window = update.size if update.size > 0 else None
            self.context_used = update.used
            return []
        if isinstance(update, schema.ToolCallStart | schema.ToolCallProgress):
            return self.end_part() + self.tool(update)
        if not isinstance(update, schema.AgentMessageChunk | schema.AgentThoughtChunk):
            return []
        value = content_value(update.content)
        thought = isinstance(update, schema.AgentThoughtChunk)
        previous = self.parts[self.streaming] if self.streaming is not None else None
        if isinstance(value, BinaryContent) and thought:
            value = f"[{value.media_type}]"
        if (
            isinstance(value, str)
            and self.streaming is not None
            and update.message_id == self.message_id
        ):
            if isinstance(previous, TextPart) and not thought:
                previous.content += value
                return [
                    PartDeltaEvent(
                        index=self.streaming, delta=TextPartDelta(content_delta=value)
                    )
                ]
            if isinstance(previous, ThinkingPart) and thought:
                previous.content += value
                return [
                    PartDeltaEvent(
                        index=self.streaming,
                        delta=ThinkingPartDelta(content_delta=value),
                    )
                ]
        events = self.end_part()
        if isinstance(value, BinaryContent):
            part = FilePart(content=value, provider_name="acp")
            index = len(self.parts)
            self.parts.append(part)
            return [
                *events,
                PartStartEvent(index=index, part=part),
                PartEndEvent(index=index, part=part),
            ]
        part = ThinkingPart(content=value) if thought else TextPart(content=value)
        self.streaming = len(self.parts)
        self.message_id = update.message_id
        self.parts.append(part)
        return [*events, PartStartEvent(index=self.streaming, part=replace(part))]

    def tool(
        self, update: schema.ToolCallStart | schema.ToolCallProgress
    ) -> list[AgentStreamEvent]:
        """Merge a tool's partial updates and retain its native call/result pair."""
        key = update.tool_call_id
        previous = self.tools.get(key)
        tool = schema.ToolCall.model_validate(
            (previous.model_dump() if previous is not None else {})
            | update.model_dump(exclude_unset=True, exclude_none=True)
        )
        self.tools[key] = tool
        raw_input = json_value_adapter.validate_python(tool.raw_input)
        args = (
            raw_input
            if isinstance(raw_input, dict | str) or raw_input is None
            else json.dumps(raw_input)
        )
        events: list[AgentStreamEvent] = []
        if key not in self.calls:
            call = NativeToolCallPart(
                tool_name=tool.title, args=args, tool_call_id=key, provider_name="acp"
            )
            self.calls[key] = call
            self.parts.append(call)
            events.append(
                FunctionToolCallEvent(
                    part=ToolCallPart(tool_name=tool.title, args=args, tool_call_id=key)
                )
            )
        else:
            self.calls[key].tool_name = tool.title
            self.calls[key].args = args
        self.calls[key].provider_details = {
            "acp": json_value_adapter.validate_json(
                tool.model_dump_json(by_alias=True, exclude_none=True)
            )
        }
        if tool.status not in ("completed", "failed"):
            return events
        output = json_value_adapter.validate_python(tool.raw_output)
        if output is None:
            output = [
                json_value_adapter.validate_json(
                    c.model_dump_json(by_alias=True, exclude_none=True)
                )
                for c in tool.content or []
            ]
        outcome = "failed" if tool.status == "failed" else "success"
        if key not in self.returns:
            result = NativeToolReturnPart(
                tool_name=tool.title,
                content=output,
                tool_call_id=key,
                outcome=outcome,
                provider_name="acp",
            )
            self.returns[key] = result
            self.parts.append(result)
        else:
            self.returns[key].content = output
            self.returns[key].outcome = outcome
        events.append(
            FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name=tool.title,
                    content=output,
                    tool_call_id=key,
                    outcome=outcome,
                )
            )
        )
        return events
