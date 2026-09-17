"""The consumed portion of ZCode desktop's bundled app-server protocol (0.16.5)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_ai.settings import ThinkingEffort

from octomate.types.json import JsonObject, JsonValue

EFFORT_LEVELS: dict[ThinkingEffort, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}

json_object_adapter = TypeAdapter(JsonObject)


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(
            validation_alias=to_camel, serialization_alias=to_camel
        ),
        populate_by_name=True,
        hide_input_in_errors=True,
    )


class ModelRef(WireModel):
    provider_id: str
    model_id: str


class ReasoningLevel(WireModel):
    value: str
    label: str


class RuntimeReasoning(WireModel):
    enabled: bool
    levels: list[ReasoningLevel]
    default_level: str | None = None

    @property
    def efforts(self) -> tuple[ThinkingEffort, ...]:
        supported = {level.value for level in self.levels} if self.enabled else set()
        return tuple(
            effort for effort, level in EFFORT_LEVELS.items() if level in supported
        )


class RuntimeModel(WireModel):
    model_id: str
    label: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_images: bool = False
    supports_pdf: bool = False
    supports_video: bool = False
    provider_options: JsonObject | None = None
    reasoning: RuntimeReasoning | None = None


class InlineApiKey(WireModel):
    source: Literal["inline"] = "inline"
    value: str = Field(repr=False)


class RuntimeProvider(WireModel):
    provider_id: str
    kind: Literal["anthropic", "openai", "openai-compatible"]
    source: Literal["custom"] = "custom"
    base_url: str = Field(validation_alias="baseURL", serialization_alias="baseURL")
    api_key: InlineApiKey
    headers: dict[str, str] = Field(repr=False)
    models: list[RuntimeModel]


class RuntimeConfig(WireModel):
    revision: str = Field(default_factory=lambda: str(uuid4()))
    generated_at: int = Field(default_factory=lambda: int(time.time() * 1000))
    model: ModelRef
    provider: RuntimeProvider
    thought_level: str | None = None


class AvailableModel(WireModel):
    ref: ModelRef
    label: str
    description: str | None = None
    reasoning: RuntimeReasoning | None = None
    disabled_reason: str | None = None


class ModelCatalog(WireModel):
    available: list[AvailableModel]


class WorkspaceState(WireModel):
    model_catalog: ModelCatalog


class DesktopReasoning(WireModel):
    enabled: bool = False
    variants: list[str] = Field(default_factory=list)
    default_variant: str | None = None


class ModelLimits(WireModel):
    context: int | None = None
    output: int | None = None


class ModelModalities(WireModel):
    input: list[str] = Field(default_factory=lambda: ["text"])


class DesktopModel(WireModel):
    name: str | None = None
    reasoning: DesktopReasoning = Field(default_factory=DesktopReasoning)
    limit: ModelLimits = Field(default_factory=ModelLimits)
    modalities: ModelModalities = Field(default_factory=ModelModalities)
    options: JsonObject = Field(default_factory=dict)


class DesktopOptions(WireModel):
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    base_url: str = Field(validation_alias="baseURL")
    headers: dict[str, str] = Field(default_factory=dict, repr=False)


class DesktopProvider(WireModel):
    kind: Literal["anthropic", "openai", "openai-compatible"]
    options: DesktopOptions
    models: dict[str, DesktopModel]
    enabled: bool = True


class DesktopConfig(WireModel):
    provider: dict[str, DesktopProvider]

    @classmethod
    def read(cls, path: Path) -> DesktopConfig:
        return cls.model_validate_json(path.read_bytes())

    def runtime_model(
        self,
        provider_id: str,
        model_id: str,
        effort: ThinkingEffort | None,
    ) -> RuntimeConfig:
        provider = self.provider.get(provider_id)
        if provider is None or not provider.enabled:
            raise ValueError(
                f"ZCode desktop provider {provider_id!r} is missing or disabled"
            )
        model = provider.models.get(model_id)
        if model is None:
            raise ValueError(
                f"ZCode desktop provider {provider_id!r} has no model {model_id!r}"
            )
        if (
            "start-plan" in provider_id
            or not provider.options.api_key.get_secret_value()
        ):
            raise ValueError(
                "ZCode requires a desktop provider with an API key; desktop-login "
                "authentication callbacks are not supported"
            )
        thought_level = (
            EFFORT_LEVELS[effort]
            if effort is not None
            else model.reasoning.default_variant
        )
        if thought_level is not None and (
            not model.reasoning.enabled or thought_level not in model.reasoning.variants
        ):
            raise ValueError(
                f"ZCode model {model_id!r} does not support thought level {thought_level!r}"
            )
        return RuntimeConfig(
            model=ModelRef(provider_id=provider_id, model_id=model_id),
            thought_level=thought_level,
            provider=RuntimeProvider(
                provider_id=provider_id,
                kind=provider.kind,
                base_url=provider.options.base_url,
                api_key=InlineApiKey(value=provider.options.api_key.get_secret_value()),
                headers=provider.options.headers,
                models=[
                    RuntimeModel(
                        model_id=model_id,
                        label=model.name,
                        context_window=model.limit.context,
                        max_output_tokens=model.limit.output,
                        supports_images="image" in model.modalities.input,
                        supports_pdf="pdf" in model.modalities.input,
                        supports_video="video" in model.modalities.input,
                        provider_options=model.options or None,
                        reasoning=RuntimeReasoning(
                            enabled=True,
                            levels=[
                                ReasoningLevel(value=level, label=level)
                                for level in model.reasoning.variants
                            ],
                            default_level=thought_level,
                        )
                        if model.reasoning.enabled
                        else None,
                    )
                ],
            ),
        )


class RpcError(WireModel):
    code: int
    message: str


class RpcResponse(WireModel):
    id: int | str
    result: JsonValue = None
    error: RpcError | None = None


class RpcRequest(WireModel):
    id: int | str
    method: str
    params: JsonObject = Field(default_factory=dict)


class InteractionOrigin(WireModel):
    kind: Literal["subagent"]
    parent_session_id: str


class PermissionParams(WireModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    turn_id: str | None = None
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    reason: str
    risk_level: Literal["low", "medium", "high", "critical"]
    input: JsonValue
    origin: InteractionOrigin | None = None


class QuestionOption(WireModel):
    value: str
    label: str
    description: str | None = None
    preview: str | None = None


class QuestionItem(WireModel):
    question: str = Field(min_length=1)
    header: str
    options: list[QuestionOption] = Field(min_length=1)
    multi_select: bool = False


class QuestionSchema(WireModel):
    tool_name: str | None = None
    interaction: Literal["plan_approval"] | None = None


class PlanInput(WireModel):
    plan: str = Field(min_length=1)


class QuestionParams(WireModel):
    request_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    turn_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    prompt: str | None = None
    questions: list[QuestionItem] = Field(default_factory=list)
    input: JsonValue = None
    origin: InteractionOrigin | None = None
    request_schema: QuestionSchema | None = Field(default=None, alias="schema")


class PermissionRequest(WireModel):
    method: Literal["interaction/requestPermission"]
    params: PermissionParams


class QuestionRequest(WireModel):
    method: Literal["interaction/requestUserInput"]
    params: QuestionParams


InteractionRequest = Annotated[
    PermissionRequest | QuestionRequest, Field(discriminator="method")
]
interaction_request_adapter = TypeAdapter(InteractionRequest)


class RpcNotification(WireModel):
    method: str
    params: JsonObject = Field(default_factory=dict)


class SessionIdentity(WireModel):
    session_id: str


class SessionSnapshot(WireModel):
    session: SessionIdentity


class TurnStarted(WireModel):
    input_id: str | None = None
    message_id: str | None = None
    execution_kind: Literal["agent", "controlOnly"] = "agent"


class Usage(WireModel):
    model_request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class TurnCompleted(WireModel):
    response: str
    result_type: Literal[
        "success",
        "cancelled",
        "error_max_turns",
        "error_max_budget",
        "error_during_execution",
        "error_max_tool_calls",
    ]
    usage: Usage = Field(default_factory=Usage)


class ErrorDetail(WireModel):
    message: str


class TurnFailed(WireModel):
    error: ErrorDetail


class ContentDelta(WireModel):
    kind: Literal["text_delta", "reasoning_delta"]
    assistant_message_id: str | None = None
    delta: str


class ToolInputStart(WireModel):
    kind: Literal["tool_input_start"]
    assistant_message_id: str | None = None
    tool_call_id: str
    tool_name: str


class ToolInputDelta(WireModel):
    kind: Literal["tool_input_delta"]
    tool_call_id: str
    delta: str


class ToolCall(WireModel):
    kind: Literal["tool_call"]
    assistant_message_id: str | None = None
    tool_call_id: str
    tool_name: str | None = None
    input: JsonObject | None = None


class StreamMarker(WireModel):
    kind: Literal[
        "start",
        "finish",
        "error",
        "text_start",
        "text_end",
        "reasoning_start",
        "reasoning_end",
        "tool_input_end",
    ]


type ModelStreaming = Annotated[
    ContentDelta | ToolInputStart | ToolInputDelta | ToolCall | StreamMarker,
    Field(discriminator="kind"),
]


class ToolResult(WireModel):
    output: JsonValue = None
    success: bool = True
    error: str | ErrorDetail | None = None


class ToolIdentity(WireModel):
    tool_call_id: str
    source: Literal["subagent"] | None = None


class ToolScheduled(ToolIdentity):
    kind: Literal["scheduled"]
    tool_name: str
    input: JsonObject | str | None = None


class ToolFinished(ToolIdentity):
    kind: Literal["result"]
    result: ToolResult


class ToolFailed(ToolIdentity):
    kind: Literal["error"]
    error: ErrorDetail


class ToolProgress(WireModel):
    kind: Literal["started", "progress", "batch", "raw"]


type ToolEvent = Annotated[
    ToolScheduled | ToolFinished | ToolFailed | ToolProgress,
    Field(discriminator="kind"),
]


class SessionEvent[PayloadT](WireModel):
    session_id: str
    event_id: str
    seq: int
    payload: PayloadT


class TurnStartedEvent(SessionEvent[TurnStarted]):
    type: Literal["turn.started"]
    turn_id: str = Field(min_length=1)


class ModelStreamingEvent(SessionEvent[ModelStreaming]):
    type: Literal["model.streaming"]
    turn_id: str | None = None


class ToolUpdatedEvent(SessionEvent[ToolEvent]):
    type: Literal["tool.updated"]
    turn_id: str | None = None


class TurnCompletedEvent(SessionEvent[TurnCompleted]):
    type: Literal["turn.completed"]
    turn_id: str | None = None


class TurnFailedEvent(SessionEvent[TurnFailed]):
    type: Literal["turn.failed"]
    turn_id: str | None = None


type RunEvent = Annotated[
    TurnStartedEvent
    | ModelStreamingEvent
    | ToolUpdatedEvent
    | TurnCompletedEvent
    | TurnFailedEvent,
    Field(discriminator="type"),
]
run_event_adapter = TypeAdapter(RunEvent)


class StateUpdated(WireModel):
    type: Literal["state.updated"]
    scope: Literal["session"]
    session_id: str
    revision: int
    reason: str | None = None


class SendAccepted(WireModel):
    accepted: Literal[True]
    session_id: str
    state_revision: int


class CacheTokens(WireModel):
    read: int = 0
    write: int = 0


class MessageTokens(WireModel):
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache: CacheTokens = Field(default_factory=CacheTokens)


class StoredModelRef(WireModel):
    provider_id: str = Field(validation_alias="providerID")
    model_id: str = Field(validation_alias="modelID")


class MessageSemantics(WireModel):
    origin: str
    kind: str
    ui_visibility: Literal["visible", "hidden", "debug"]
    transcript_visibility: Literal["visible", "hidden"]
    provider_visibility: Literal["visible", "hidden"]


class MessageInfo(WireModel):
    message_id: str = Field(validation_alias="id")
    synthetic: bool = False
    visibility: Literal["user-visible", "model-only"] = "user-visible"
    semantics: MessageSemantics | None = None

    @property
    def visible(self) -> bool:
        return (
            not self.synthetic
            and self.visibility != "model-only"
            and (
                self.semantics is None
                or (
                    self.semantics.ui_visibility == "visible"
                    and self.semantics.transcript_visibility == "visible"
                    and self.semantics.kind != "timeline_event"
                    and (
                        self.semantics.origin != "agent_runtime"
                        or (
                            isinstance(self, AssistantMessageInfo)
                            and self.semantics.kind == "assistant_response"
                        )
                    )
                )
            )
        )


class UserMessageInfo(MessageInfo):
    role: Literal["user"]
    model: StoredModelRef


class StoredError(WireModel):
    name: str
    data: JsonObject = Field(default_factory=dict)


class AssistantMessageInfo(MessageInfo):
    role: Literal["assistant"]
    model_id: str = Field(validation_alias="modelID")
    provider_id: str = Field(validation_alias="providerID")
    finish: str | None = None
    error: StoredError | None = None
    tokens: MessageTokens = Field(default_factory=MessageTokens)


class TextPart(WireModel):
    type: Literal["text"]
    text: str
    synthetic: bool = False
    ignored: bool = False


class ReasoningPart(WireModel):
    type: Literal["reasoning"]
    text: str


class PendingTool(WireModel):
    status: Literal["pending", "running"]
    input: JsonObject


class CompletedTool(WireModel):
    status: Literal["completed"]
    input: JsonObject
    output: str


class FailedTool(WireModel):
    status: Literal["error"]
    input: JsonObject
    error: str


class ToolPart(WireModel):
    type: Literal["tool"]
    call_id: str = Field(validation_alias="callID")
    tool: str
    state: Annotated[
        PendingTool | CompletedTool | FailedTool, Field(discriminator="status")
    ]


type MessagePart = Annotated[
    TextPart | ReasoningPart | ToolPart, Field(discriminator="type")
]


class HistoryMessage(WireModel):
    info: Annotated[UserMessageInfo | AssistantMessageInfo, Field(discriminator="role")]
    parts: list[MessagePart]

    @field_validator("parts", mode="before")
    @classmethod
    def consumed_parts(cls, value: JsonValue) -> JsonValue:
        if not isinstance(value, list):
            return value
        return [
            part
            for part in value
            if not isinstance(part, dict)
            or part.get("type") in {"text", "reasoning", "tool"}
        ]


class SessionMessages(WireModel):
    messages: list[HistoryMessage]
