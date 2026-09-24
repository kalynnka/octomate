"""The consumed dsh Remote API contracts and Octomate's normalized stream frames.

Upstream sources: packages/api/gateway/src/stream-protocol.ts and
packages/api/session-controller/src/types.ts. Transport envelopes are validated;
merge-extensible session events preserve unknown fields in replay metadata.
"""

from __future__ import annotations

from typing import Annotated, Literal

from octomate_protocol.deepseek import PERMISSIVE, RpcError
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
)
from typing_extensions import TypedDict

from octomate.types.json import JsonValue
from octomate.types.permissions import PermissionMode


class RpcReceipt(BaseModel):
    """Local receipt for a Remote event-result call."""

    model_config = PERMISSIVE

    accepted: bool
    reason: str | None = None


class SessionEvent(BaseModel):
    """One entry in a dsh session log.

    `type` is deliberately a plain `str`: dsh's vocabulary grows, and an event
    this build has never heard of must flow through to be ignored rather than
    fail the turn. `data` stays `JsonValue` for the same reason — the readers
    below narrow only the shape each consumer needs.
    """

    model_config = PERMISSIVE

    type: str
    seq: int
    time: float
    data: JsonValue = None
    ignorable: bool = False


class SessionEventFrame(BaseModel):
    """A mux frame carrying one session event, plus its render view when present."""

    model_config = PERMISSIVE

    type: Literal["session/event"]
    session_id: str
    event: SessionEvent
    view: JsonValue = None


class ApprovalRequestedFrame(BaseModel):
    """A mux frame asking approval for a tool call mid-turn."""

    model_config = PERMISSIVE

    type: Literal["approval/requested"]
    session_id: str
    approval_id: str
    tool_name: str
    call_id: str | None = None
    reason: str | None = None


class AskQuestionOption(BaseModel):
    """One choice on a dsh `ask()` question."""

    model_config = PERMISSIVE

    label: str
    description: str | None = None


class AskQuestionItem(BaseModel):
    """One question inside a dsh `ask()` batch. Answers are matched back by
    option *label*, so the labels must be echoed pristine."""

    model_config = PERMISSIVE

    id: str
    question: str
    detail: str | None = None
    header: str | None = None
    options: list[AskQuestionOption] | None = None
    multi_select: bool = False


class QuestionRequestedFrame(BaseModel):
    """A mux frame carrying a dsh `ask()` batch to answer."""

    model_config = PERMISSIVE

    type: Literal["question/requested"]
    session_id: str
    questions: list[AskQuestionItem]


class StreamErrorFrame(BaseModel):
    """A mux frame reporting a stream error, for one session when it names one."""

    model_config = PERMISSIVE

    type: Literal["stream/error"]
    error: RpcError
    session_id: str | None = None


class EventStreamArgs(TypedDict):
    pass


class EventStreamPayload(TypedDict):
    args: EventStreamArgs


class RemoteEventsOpen(BaseModel):
    model_config = PERMISSIVE

    type: Literal["open"] = "open"
    stream_id: Literal["$events"] = "$events"
    endpoint: Literal["$events"] = "$events"
    payload: EventStreamPayload = Field(default_factory=lambda: {"args": {}})


class RemoteCancel(BaseModel):
    model_config = PERMISSIVE

    type: Literal["cancel"] = "cancel"
    stream_id: str


class RemoteReady(BaseModel):
    """The `$events` stream's opening item, assigning this client its id."""

    model_config = PERMISSIVE

    type: Literal["ready"]
    client_id: str


class RemoteInvocation(BaseModel):
    """An event dsh delegates to this client to answer, pushed on `$events`."""

    model_config = PERMISSIVE

    type: Literal["waterfall"]
    event: str
    event_id: str
    agent_id: str
    request: dict[str, JsonValue]


class RemoteCancellation(BaseModel):
    """Cancellation of a delegated event, by its id."""

    model_config = PERMISSIVE

    type: Literal["cancel"]
    event_id: str


class RemoteNotification(BaseModel):
    type: Literal["emit"]
    event: str
    args: list[JsonValue]


remote_event_adapter = TypeAdapter(
    Annotated[
        RemoteInvocation | RemoteCancellation | RemoteNotification,
        Field(discriminator="type"),
    ]
)


class SessionSnapshot(BaseModel):
    """A `session/follow` stream's snapshot marker, after which the follow is live."""

    type: Literal["snapshot"]
    cursor: int


class SessionRecord(BaseModel):
    """One session event on a `session/follow` stream."""

    type: Literal["event"]
    event: SessionEvent


class AssistantStreamChunk(BaseModel):
    """One chunk of an assistant attempt's live stream."""

    model_config = PERMISSIVE

    type: Literal["chunk"]
    attempt_id: str
    revision: int
    index: int
    time: float
    chunk: JsonValue


class AssistantStreamBoundary(BaseModel):
    """The start or end of an assistant attempt's live stream."""

    model_config = PERMISSIVE

    type: Literal["start", "end"]
    attempt_id: str
    revision: int


class SessionAssistantFrame(BaseModel):
    """A mux frame relaying an assistant-stream chunk or boundary."""

    type: Literal["assistant-stream"]
    frame: Annotated[
        AssistantStreamChunk | AssistantStreamBoundary, Field(discriminator="type")
    ]
    session_id: str = ""


session_follow_adapter = TypeAdapter(
    Annotated[
        SessionSnapshot | SessionRecord | SessionAssistantFrame,
        Field(discriminator="type"),
    ]
)


type MuxFrame = (
    SessionEventFrame
    | SessionAssistantFrame
    | ApprovalRequestedFrame
    | QuestionRequestedFrame
    | StreamErrorFrame
    | RemoteCancellation
)


class DeepseekUsage(BaseModel):
    """The `usage` slot dsh attaches when the model adapter reported one."""

    model_config = PERMISSIVE

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


class TextDeltaChunk(BaseModel):
    """A streamed text delta."""

    model_config = PERMISSIVE

    type: Literal["text-delta"]
    text: str


class ReasoningDeltaChunk(BaseModel):
    """A streamed reasoning delta."""

    model_config = PERMISSIVE

    type: Literal["reasoning-delta"]
    text: str


class ToolCallDeltaChunk(BaseModel):
    """A streamed tool-call delta: the call id and its name or argument fragment."""

    model_config = PERMISSIVE

    type: Literal["tool-call-delta"]
    id: str
    name: str | None = None
    arguments_delta: str = ""


class UsageChunk(BaseModel):
    """A streamed usage report."""

    model_config = PERMISSIVE

    type: Literal["usage"]
    usage: DeepseekUsage


type StreamDelta = (
    TextDeltaChunk | ReasoningDeltaChunk | ToolCallDeltaChunk | UsageChunk
)


class ChunkEnvelope(BaseModel):
    """The data of an `assistant/chunk` event: one discriminated stream delta."""

    model_config = PERMISSIVE

    chunk: Annotated[StreamDelta, Field(discriminator="type")]


chunk_envelope_adapter: TypeAdapter[ChunkEnvelope] = TypeAdapter(ChunkEnvelope)


def chunk_delta(event: SessionEvent) -> StreamDelta | None:
    """Narrows an `assistant/chunk` event to a delta the tentacle renders.

    Like every reader below, this answers "is this the shape I need?" and
    returns None when it is not: an unrecognised chunk kind degrades to
    not-rendered rather than to an exception inside the stream fold.
    """
    try:
        return chunk_envelope_adapter.validate_python(event.data).chunk
    except ValidationError:
        return None


class ContentBlock(BaseModel):
    """One block of a message's content; only text blocks are read."""

    model_config = PERMISSIVE

    type: str = ""
    text: str | None = None


def text_of(blocks: list[ContentBlock]) -> str:
    return "".join(
        block.text
        for block in blocks
        if block.type == "text" and block.text is not None
    )


class ModelRoute(BaseModel):
    """One exact model route, the pair dsh identifies a model by everywhere."""

    model_config = PERMISSIVE

    provider: str
    model: str


class ModelEffort(BaseModel):
    """One reasoning effort level a catalog model offers."""

    id: str


class ModelReasoning(BaseModel):
    """A catalog model's reasoning effort levels."""

    efforts: list[ModelEffort]


class CatalogModel(BaseModel):
    """One model in dsh's catalog."""

    id: str
    name: str
    description: str | None = None
    reasoning: ModelReasoning | None = None


class ModelProviderGroup(BaseModel):
    """One provider's models in dsh's catalog."""

    id: str
    name: str
    models: list[CatalogModel]


class ModelCatalogFailure(BaseModel):
    """A provider dsh could not list, and why."""

    id: str
    name: str
    message: str


class ModelCatalog(BaseModel):
    """dsh's model catalog: the default route, models by provider, and failures."""

    default: ModelRoute
    groups: list[ModelProviderGroup]
    failures: list[ModelCatalogFailure]


class MessageBody(BaseModel):
    """A message's content blocks and provenance `source`."""

    model_config = PERMISSIVE

    content: list[ContentBlock] = Field(default_factory=list)
    source: JsonValue = None


class AssistantMessageData(BaseModel):
    """An `assistant/message` event's data: the committed message and its usage."""

    model_config = PERMISSIVE

    message: MessageBody
    usage: DeepseekUsage | None = None


assistant_message_adapter: TypeAdapter[AssistantMessageData] = TypeAdapter(
    AssistantMessageData
)
model_route_adapter: TypeAdapter[ModelRoute] = TypeAdapter(ModelRoute)


def assistant_message_of(event: SessionEvent) -> AssistantMessageData | None:
    """The committed body of an `assistant/message` event, or None."""
    try:
        return assistant_message_adapter.validate_python(event.data)
    except ValidationError:
        return None


def provenance_of(event: SessionEvent) -> ModelRoute | None:
    """The route that produced an `assistant/message`, from its `model` source.

    dsh stamps provenance on the message itself rather than leaving it to be
    inferred from the session's current selection — the difference that matters
    when the model was switched part-way through a session."""
    message = assistant_message_of(event)
    if message is None:
        return None
    try:
        return model_route_adapter.validate_python(message.message.source)
    except ValidationError:
        return None


def request_route_of(event: SessionEvent) -> ModelRoute | None:
    """The route a `request/context` names. This lands before the step it
    describes, so it is the only reader that can name the model of a turn that
    ended without ever committing an assistant message."""
    try:
        return model_route_adapter.validate_python(event.data)
    except ValidationError:
        return None


class TurnEndReasonData(BaseModel):
    """Why a turn ended. `kind` stays `str` — the reason map is
    merge-extensible upstream, so a plugin's kind must carry, not fail."""

    model_config = PERMISSIVE

    kind: str = "completed"
    # The structured `LlmFailure` when kind is "error".
    error: JsonValue = None

    @property
    def error_message(self) -> str | None:
        if isinstance(self.error, dict):
            message = self.error.get("message")
            if isinstance(message, str):
                return message
        return None


class TurnEndData(BaseModel):
    """A `turn/end` event's data: the turn number and why it ended."""

    model_config = PERMISSIVE

    turn: int | None = None
    reason: TurnEndReasonData | None = None


turn_end_adapter: TypeAdapter[TurnEndData] = TypeAdapter(TurnEndData)


def turn_end_of(event: SessionEvent) -> TurnEndData | None:
    try:
        return turn_end_adapter.validate_python(event.data)
    except ValidationError:
        return None


class ToolCallData(BaseModel):
    """A `tool/call` event's identity and raw arguments. `arguments` is the
    raw JSON string the model produced, unparsed — dsh does not parse it either."""

    model_config = PERMISSIVE

    call_id: str
    name: str
    arguments: str = ""


tool_call_adapter: TypeAdapter[ToolCallData] = TypeAdapter(ToolCallData)


def tool_call_of(event: SessionEvent) -> ToolCallData | None:
    try:
        return tool_call_adapter.validate_python(event.data)
    except ValidationError:
        return None


class ToolResultBlock(BaseModel):
    """One tool-result block: the call answered, its content, and its error flag."""

    model_config = PERMISSIVE

    tool_call_id: str
    content: list[ContentBlock] = Field(default_factory=list)
    is_error: bool = False


class ToolResultData(BaseModel):
    """A `tool/result` event's raw data, narrowed further by `tool_result_of`."""

    model_config = PERMISSIVE

    # Only the first block is typed — the rest of the list is whatever dsh put
    # there, and reading past [0] is not something any consumer of ours does.
    message: JsonValue = None
    error: JsonValue = None


tool_result_data_adapter: TypeAdapter[ToolResultData] = TypeAdapter(ToolResultData)
tool_result_block_adapter: TypeAdapter[ToolResultBlock] = TypeAdapter(ToolResultBlock)


class DeepseekToolResult(BaseModel):
    """A tool result reduced to what a return card shows."""

    call_id: str
    # The model-facing result text.
    text: str
    is_error: bool


def tool_result_of(event: SessionEvent) -> DeepseekToolResult | None:
    """A `tool/result` event, reduced to what a return card shows."""
    try:
        data = tool_result_data_adapter.validate_python(event.data)
    except ValidationError:
        return None
    if not isinstance(data.message, dict):
        return None
    content = data.message.get("content")
    if not isinstance(content, list) or not content:
        return None
    try:
        block = tool_result_block_adapter.validate_python(content[0])
    except ValidationError:
        return None
    return DeepseekToolResult(
        call_id=block.tool_call_id,
        text=text_of(block.content),
        is_error=block.is_error or isinstance(data.error, dict),
    )


class UserMessageSource(BaseModel):
    """A `user/message` event's provenance. `rpc_id` is stamped by the /api
    gateway on prompts submitted through it; a prompt typed into a local
    CLI/TUI carries a bare `{kind: 'user'}`."""

    model_config = PERMISSIVE

    kind: str = "user"
    rpc_id: str | None = None


class UserMessageData(BaseModel):
    """A `user/message` event's data: content blocks and provenance."""

    model_config = PERMISSIVE

    content: list[ContentBlock] = Field(default_factory=list)
    source: UserMessageSource = Field(default_factory=UserMessageSource)


user_message_adapter: TypeAdapter[UserMessageData] = TypeAdapter(UserMessageData)


def user_message_of(event: SessionEvent) -> UserMessageData | None:
    """The body of a `user/message` event — a human prompt in the session log."""
    try:
        return user_message_adapter.validate_python(event.data)
    except ValidationError:
        return None


class PermissionCatalog(BaseModel):
    """The permission presets dsh offers, as `permissionPresets/catalog` lists them."""

    options: tuple[PermissionMode, ...]


class PermissionPresetData(BaseModel):
    """A `permission/preset` event's data: the preset switched to."""

    model_config = PERMISSIVE

    preset: str


permission_preset_adapter: TypeAdapter[PermissionPresetData] = TypeAdapter(
    PermissionPresetData
)


def permission_preset_of(event: SessionEvent) -> str | None:
    """The preset a `permission/preset` event says the session switched to."""
    try:
        return permission_preset_adapter.validate_python(event.data).preset
    except ValidationError:
        return None


class HistoryEntry(BaseModel):
    """One `session/page` record — and the line shape `octomate deepseek tail`
    streams in: the raw persisted event plus the host-computed render `view`
    when a presenter produced one (pagination-time, never stored)."""

    model_config = PERMISSIVE

    event: SessionEvent
    view: JsonValue = None


history_entry_adapter: TypeAdapter[HistoryEntry] = TypeAdapter(HistoryEntry)


class SessionTitle(BaseModel):
    """The title in a `session/title` event or the session's projection values."""

    title: str | None = None


class SessionProjectionsValue(BaseModel):
    """The current values returned by `session/projections`."""

    values: SessionTitle


class SessionCreateValue(BaseModel):
    """The value `session/create` answers with."""

    model_config = PERMISSIVE

    session_id: str
    agent_preset: str | None = None


class SessionPromptValue(BaseModel):
    """The value `session/prompt` answers with: the prompt was accepted."""

    accepted: Literal[True]


class CommandResult(BaseModel):
    """What an executed command produced: its kind and text."""

    model_config = PERMISSIVE

    kind: str
    text: str | None = None


class CommandExecutionValue(BaseModel):
    """What the remotes-plane `commands/execute` produced. A null wire value —
    no command matched the line — parses at the call site, not here."""

    model_config = PERMISSIVE

    command_id: str
    result: CommandResult | None = None
