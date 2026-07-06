from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import (
    Annotated,
    Literal,
    NotRequired,
    TypeAlias,
    TypedDict,
    cast,
)

from arcanus import (
    BaseTransmuter,
    Relation,
    RelationCollection,
    Relationship,
    Relationships,
)
from arcanus.base import Identity
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from uuid_utils.compat import uuid7

from octomate.models import deferred as deferred_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import ResponseTargetMode, SummonDecision
from octomate.types.deferred import (
    DeferredActionStatus,
    DeferredBatchStatus,
)
from octomate.types.json import JsonObject


class QuestionRequest(TypedDict):
    question: str
    choices: NotRequired[
        Annotated[
            list[str] | None,
            Field(
                max_length=3,
                description=(
                    "Up to 3 suggested choices. The user can still answer with text."
                ),
            ),
        ]
    ]
    hint: NotRequired[str]


class ApprovalRequest(BaseModel):
    tool_name: str
    args: JsonObject = Field(default_factory=dict)
    title: str = "Permission Required"
    description: str = ""


class ApprovalRequestPayload(TypedDict):
    tool_name: str
    args: JsonObject


class DeferredQuestionPayload(TypedDict):
    kind: Literal["question"]
    tool_name: str
    tool_call_id: str
    position: int
    args: QuestionRequest
    metadata: JsonObject


class DeferredApprovalPayload(TypedDict):
    kind: Literal["approval"]
    tool_name: str
    tool_call_id: str
    args: ApprovalRequest | ApprovalRequestPayload
    metadata: JsonObject


DeferredActionPayload: TypeAlias = DeferredQuestionPayload | DeferredApprovalPayload
DeferredActionCollectionInput: TypeAlias = (
    DeferredToolRequests | list[DeferredActionPayload | JsonObject]
)
DeferredQuestionResult: TypeAlias = str | None
DeferredApprovalResult: TypeAlias = bool | None


@sqlalchemy_materia.bless(deferred_models.DeferredAction)
class DeferredAction(BaseTransmuter):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    batch_id: uuid.UUID | None = None
    status: DeferredActionStatus = "pending"
    tool_name: str
    tool_call_id: str
    position: int = 0
    metadata: JsonObject = Field(
        default_factory=dict,
        validation_alias=AliasChoices("meta", "metadata"),
    )
    result: DeferredQuestionResult | DeferredApprovalResult = None
    platform_message_id: str | None = None
    responder_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None

    batch: Relation[DeferredActionBatch] = Relationship()

    @property
    def resolved(self) -> bool:
        return self.status in {"answered", "approved", "denied", "expired", "failed"}


# `kind` and `args` live on the concrete variants, not the polymorphic-abstract base:
# each variant pins `kind` to its discriminator literal and narrows `args` to the
# concrete request. Keeping them off the base means there is no inherited field (nor
# arcanus column attribute) for these to shadow.
@sqlalchemy_materia.bless(deferred_models.DeferredQuestionAction)
class DeferredQuestion(DeferredAction):
    kind: Literal["question"] = "question"
    args: QuestionRequest

    def __lt__(self, other: DeferredQuestion) -> bool:
        return self.position < other.position

    def __gt__(self, other: DeferredQuestion) -> bool:
        return self.position > other.position


@sqlalchemy_materia.bless(deferred_models.DeferredApprovalAction)
class DeferredApproval(DeferredAction):
    kind: Literal["approval"] = "approval"
    args: ApprovalRequest


DeferredActionVariant: TypeAlias = Annotated[
    DeferredQuestion | DeferredApproval,
    Field(discriminator="kind"),
]
DeferredActionVariantAdapter = TypeAdapter(DeferredActionVariant)


def from_deferred_requests(
    request: DeferredActionCollectionInput,
) -> DeferredActionCollectionInput:
    if not isinstance(request, DeferredToolRequests):
        return request
    action_payloads: list[DeferredActionPayload | JsonObject] = []
    for call in request.calls:
        metadata = cast(JsonObject, request.metadata.get(call.tool_call_id, {}) or {})
        args = cast(JsonObject, call.args_as_dict())
        questions_value = args.get("questions") or []
        if not isinstance(questions_value, list):
            raise ValueError(f"deferred call {call.tool_call_id!r} has no questions")
        questions = cast(list[QuestionRequest], questions_value)
        if not questions:
            raise ValueError(f"deferred call {call.tool_call_id!r} has no questions")
        action_payloads.extend(
            DeferredQuestionPayload(
                kind="question",
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                position=position,
                args=question,
                metadata=metadata,
            )
            for position, question in enumerate(questions)
        )
    action_payloads.extend(
        DeferredApprovalPayload(
            kind="approval",
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            args=ApprovalRequestPayload(
                tool_name=call.tool_name,
                args=cast(JsonObject, call.args_as_dict()),
            ),
            metadata=cast(JsonObject, request.metadata.get(call.tool_call_id, {}) or {}),
        )
        for call in request.approvals
    )
    return action_payloads


DeferredActionCollection = TypeAdapter(
    Annotated[
        list[DeferredActionVariant],
        BeforeValidator(from_deferred_requests),
    ]
)


@sqlalchemy_materia.bless(deferred_models.DeferredActionBatch)
class DeferredActionBatch(BaseTransmuter):
    model_config = ConfigDict(from_attributes=True)

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    conversation_id: uuid.UUID
    agent_tentacle_id: str
    run_name: str | None = "react"
    status: DeferredBatchStatus = "pending"
    source_address: ChannelAddress
    target_address: ChannelAddress
    target_mode: ResponseTargetMode = "main"
    decision: SummonDecision | None = None
    requests: DeferredToolRequests
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    questions: RelationCollection[DeferredQuestion] = Relationships()
    approvals: RelationCollection[DeferredApproval] = Relationships()

    @property
    def completed(self) -> bool:
        return all(action.resolved for action in self.questions) and all(
            action.resolved for action in self.approvals
        )

    def build_results(self) -> DeferredToolResults:
        results = DeferredToolResults()
        question_actions: dict[str, list[DeferredQuestion]] = {}
        for action in self.questions:
            question_actions.setdefault(action.tool_call_id, []).append(action)
            if action.metadata:
                results.metadata[action.tool_call_id] = action.metadata
        for action in self.approvals:
            results.approvals[action.tool_call_id] = bool(action.result)
            if action.metadata:
                results.metadata[action.tool_call_id] = action.metadata
        for tool_call_id, actions in question_actions.items():
            results.calls[tool_call_id] = [
                "" if action.result is None else str(action.result)
                for action in sorted(actions)
            ]
        return results
