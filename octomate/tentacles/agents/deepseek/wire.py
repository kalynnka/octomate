"""The dsh `/api` wire contract, as much of it as the tentacle consumes.

Hand-mirrored from dsh's own `api/` layer (via the VS Code extension's
`src/dsh/wire.ts`, the reference port) rather than generated or imported: the
tentacle drives *the operator's* dsh, whose version octomate does not choose
and cannot pin, so every type here is permissive — unknown frame types,
unknown event types and unknown fields are carried, not rejected. Strict
parsing would turn every version skew into a dead agent.

The upstream source of truth, for anyone re-syncing this file:
`packages/host/apiproxy/src/api/{rpc,events,sessions}.ts` in the dsh monorepo.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from pydantic.alias_generators import to_camel

from octomate.types.json import JsonValue

# Wire names are camelCase; models keep snake_case attributes behind generated
# validation/serialization aliases, and `populate_by_name` keeps construction
# from Python readable. Deliberately not a plain `alias`: that would also rename
# the synthesized `__init__` parameters under pyright, and the two halves are
# all the wire needs. `extra="allow"` is the carried-not-rejected posture: an
# unknown field survives a round-trip into replay metadata instead of vanishing.
PERMISSIVE = ConfigDict(
    extra="allow",
    populate_by_name=True,
    alias_generator=AliasGenerator(
        validation_alias=to_camel, serialization_alias=to_camel
    ),
)


class RpcError(BaseModel):
    model_config = PERMISSIVE

    code: str
    message: str
    details: JsonValue = None


class OkResult(BaseModel):
    model_config = PERMISSIVE

    ok: Literal[True] = True
    value: JsonValue = None


class ErrResult(BaseModel):
    model_config = PERMISSIVE

    ok: Literal[False] = False
    error: RpcError


# Not discriminated: `ok` is a bool literal, which pydantic's smart union already
# matches exactly, and the pair has no other overlap.
RpcResult: TypeAlias = OkResult | ErrResult


class ClientRequest(BaseModel):
    """POST /api/<method> body."""

    model_config = PERMISSIVE

    type: Literal["client-request"] = "client-request"
    rpc_id: str
    method: str
    payload: JsonValue = None


class ServerResponse(BaseModel):
    """The body that POST answers with. A response echoes its request's rpcId."""

    model_config = PERMISSIVE

    type: Literal["server-response"] = "server-response"
    rpc_id: str
    result: RpcResult


class ServerRequest(BaseModel):
    """One downstream WebSocket frame; `payload` is a mux (or host) frame."""

    model_config = PERMISSIVE

    type: Literal["server-request"] = "server-request"
    rpc_id: str
    method: str
    payload: JsonValue = None


class ClientResponse(BaseModel):
    """POST /api/respond body — answers an answerable frame by echoing its rpcId."""

    model_config = PERMISSIVE

    type: Literal["client-response"] = "client-response"
    rpc_id: str
    result: RpcResult


class RpcReceipt(BaseModel):
    """What /api/respond answers with. `not-pending` in `reason` is a normal
    race (another client answered, or the turn was cancelled), not an error."""

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
    model_config = PERMISSIVE

    type: Literal["session/event"]
    session_id: str
    event: SessionEvent
    view: JsonValue = None


class SessionSubscribedFrame(BaseModel):
    model_config = PERMISSIVE

    type: Literal["session/subscribed"]
    session_id: str
    last_seq: int


class ApprovalRequestedFrame(BaseModel):
    model_config = PERMISSIVE

    type: Literal["approval/requested"]
    session_id: str
    approval_id: str
    tool_name: str
    call_id: str | None = None
    reason: str | None = None


class ApprovalResolvedFrame(BaseModel):
    model_config = PERMISSIVE

    type: Literal["approval/resolved"]
    session_id: str
    approval_id: str
    outcome: str


class AskQuestionOption(BaseModel):
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
    model_config = PERMISSIVE

    type: Literal["question/requested"]
    session_id: str
    questions: list[AskQuestionItem]


class QuestionResolvedFrame(BaseModel):
    model_config = PERMISSIVE

    type: Literal["question/resolved"]
    session_id: str
    question_rpc_id: str
    outcome: str


class StreamErrorFrame(BaseModel):
    model_config = PERMISSIVE

    type: Literal["stream/error"]
    error: RpcError


class UnknownFrame(BaseModel):
    """A frame this build has never heard of: data, not a parse failure."""

    model_config = PERMISSIVE

    type: str
    session_id: str | None = None


KnownMuxFrame: TypeAlias = Annotated[
    SessionEventFrame
    | SessionSubscribedFrame
    | ApprovalRequestedFrame
    | ApprovalResolvedFrame
    | QuestionRequestedFrame
    | QuestionResolvedFrame
    | StreamErrorFrame,
    Field(discriminator="type"),
]
MuxFrame: TypeAlias = KnownMuxFrame | UnknownFrame

known_mux_frame_adapter: TypeAdapter[KnownMuxFrame] = TypeAdapter(KnownMuxFrame)
unknown_frame_adapter: TypeAdapter[UnknownFrame] = TypeAdapter(UnknownFrame)


def parse_mux_frame(payload: JsonValue) -> MuxFrame:
    """The pydantic rendering of the wire's open union: a frame the known
    adapter refuses — an unknown `type`, or a known one whose shape moved —
    falls back to `UnknownFrame` rather than failing the stream. Raises
    `ValidationError` only for a payload that is not a typed frame at all."""
    try:
        return known_mux_frame_adapter.validate_python(payload)
    except ValidationError:
        return unknown_frame_adapter.validate_python(payload)


class DeepseekUsage(BaseModel):
    """The `usage` slot dsh attaches when the model adapter reported one."""

    model_config = PERMISSIVE

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


class TextDeltaChunk(BaseModel):
    model_config = PERMISSIVE

    type: Literal["text-delta"]
    text: str


class ReasoningDeltaChunk(BaseModel):
    model_config = PERMISSIVE

    type: Literal["reasoning-delta"]
    text: str


class ToolCallDeltaChunk(BaseModel):
    model_config = PERMISSIVE

    type: Literal["tool-call-delta"]
    id: str
    name: str | None = None
    arguments_delta: str = ""


class UsageChunk(BaseModel):
    model_config = PERMISSIVE

    type: Literal["usage"]
    usage: DeepseekUsage


StreamDelta: TypeAlias = (
    TextDeltaChunk | ReasoningDeltaChunk | ToolCallDeltaChunk | UsageChunk
)


class ChunkEnvelope(BaseModel):
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


class MessageBody(BaseModel):
    model_config = PERMISSIVE

    content: list[ContentBlock] = Field(default_factory=list)
    source: JsonValue = None


class AssistantMessageData(BaseModel):
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
    model_config = PERMISSIVE

    tool_call_id: str
    content: list[ContentBlock] = Field(default_factory=list)
    is_error: bool = False


class ToolResultData(BaseModel):
    model_config = PERMISSIVE

    # Only the first block is typed — the rest of the list is whatever dsh put
    # there, and reading past [0] is not something any consumer of ours does.
    message: JsonValue = None
    error: JsonValue = None


tool_result_data_adapter: TypeAdapter[ToolResultData] = TypeAdapter(ToolResultData)
tool_result_block_adapter: TypeAdapter[ToolResultBlock] = TypeAdapter(ToolResultBlock)


class DeepseekToolResult(BaseModel):
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


class PermissionPresetData(BaseModel):
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
    """One `session.history` row — and the line shape `octomate deepseek tail`
    streams in: the raw persisted event plus the host-computed render `view`
    when a presenter produced one (pagination-time, never stored)."""

    model_config = PERMISSIVE

    event: SessionEvent
    view: JsonValue = None


history_entry_adapter: TypeAdapter[HistoryEntry] = TypeAdapter(HistoryEntry)


class SessionCreateValue(BaseModel):
    model_config = PERMISSIVE

    session_id: str
    agent_preset: str | None = None


class PromptCommand(BaseModel):
    model_config = PERMISSIVE

    kind: str
    text: str | None = None


class SessionPromptValue(BaseModel):
    model_config = PERMISSIVE

    accepted: bool
    # Set when dsh intercepted the prompt as a slash command: no turn opened.
    command: PromptCommand | None = None


class CommandResult(BaseModel):
    model_config = PERMISSIVE

    kind: str
    text: str | None = None


class CommandExecutionValue(BaseModel):
    """What the remotes-plane `commands/execute` produced. A null wire value —
    no command matched the line — parses at the call site, not here."""

    model_config = PERMISSIVE

    command_id: str
    result: CommandResult | None = None
