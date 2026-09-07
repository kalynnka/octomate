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
)
from pydantic.alias_generators import to_camel
from pydantic_ai.settings import ThinkingEffort

from octomate.types.json import JsonObject, JsonValue

json_object_adapter = TypeAdapter(JsonObject)


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(
            validation_alias=to_camel, serialization_alias=to_camel
        ),
        populate_by_name=True,
        hide_input_in_errors=True,
    )


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
    ) -> JsonObject:
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
            {
                "minimal": "low",
                "low": "low",
                "medium": "high",
                "high": "high",
                "xhigh": "max",
            }[effort]
            if effort is not None
            else model.reasoning.default_variant
        )
        if thought_level is not None and (
            not model.reasoning.enabled or thought_level not in model.reasoning.variants
        ):
            raise ValueError(
                f"ZCode model {model_id!r} does not support thought level {thought_level!r}"
            )
        runtime_model: JsonObject = {"modelId": model_id}
        if model.name:
            runtime_model["label"] = model.name
        if model.limit.context is not None:
            runtime_model["contextWindow"] = model.limit.context
        if model.limit.output is not None:
            runtime_model["maxOutputTokens"] = model.limit.output
        for modality, key in [
            ("image", "supportsImages"),
            ("pdf", "supportsPdf"),
            ("video", "supportsVideo"),
        ]:
            runtime_model[key] = modality in model.modalities.input
        if model.options:
            runtime_model["providerOptions"] = model.options
        if model.reasoning.enabled:
            runtime_model["reasoning"] = {
                "enabled": True,
                "levels": [
                    {"value": level, "label": level}
                    for level in model.reasoning.variants
                ],
                **({"defaultLevel": thought_level} if thought_level else {}),
            }
        result: JsonObject = {
            "revision": str(uuid4()),
            "generatedAt": int(time.time() * 1000),
            "model": {"providerId": provider_id, "modelId": model_id},
            "provider": {
                "providerId": provider_id,
                "kind": provider.kind,
                "source": "custom",
                "baseURL": provider.options.base_url,
                "apiKey": {
                    "source": "inline",
                    "value": provider.options.api_key.get_secret_value(),
                },
                "headers": dict(provider.options.headers),
                "models": [runtime_model],
            },
        }
        if thought_level is not None:
            result["thoughtLevel"] = thought_level
        return result


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


class SessionEvent(WireModel):
    session_id: str
    turn_id: str | None = None
    event_id: str
    seq: int
    type: str
    payload: JsonObject = Field(default_factory=dict)


class SessionIdentity(WireModel):
    session_id: str


class SessionSnapshot(WireModel):
    session: SessionIdentity


class TurnStarted(WireModel):
    input_id: str | None = None
    message_id: str


class Usage(WireModel):
    model_request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class TurnCompleted(WireModel):
    response: str
    result_type: str
    usage: Usage = Field(default_factory=Usage)


class ErrorDetail(WireModel):
    message: str


class TurnFailed(WireModel):
    error: ErrorDetail


class ModelStreaming(WireModel):
    kind: str
    assistant_message_id: str | None = None
    delta: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    input: JsonValue = None


class ToolResult(WireModel):
    output: JsonValue = None
    success: bool = True
    error: str | ErrorDetail | None = None


class ToolEvent(WireModel):
    kind: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    source: str | None = None
    input: JsonValue = None
    result: ToolResult | None = None
    error: ErrorDetail | None = None


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


class UserMessageInfo(WireModel):
    message_id: str = Field(validation_alias="id")
    role: Literal["user"]
    model: StoredModelRef


class AssistantMessageInfo(WireModel):
    message_id: str = Field(validation_alias="id")
    role: Literal["assistant"]
    model_id: str = Field(validation_alias="modelID")
    provider_id: str = Field(validation_alias="providerID")
    finish: str | None = None
    tokens: MessageTokens = Field(default_factory=MessageTokens)


class TextPart(WireModel):
    type: Literal["text"]
    text: str


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


message_part_adapter = TypeAdapter(
    Annotated[TextPart | ReasoningPart | ToolPart, Field(discriminator="type")]
)


class HistoryMessage(WireModel):
    info: Annotated[UserMessageInfo | AssistantMessageInfo, Field(discriminator="role")]
    parts: list[JsonObject]


class SessionMessages(WireModel):
    messages: list[HistoryMessage]
