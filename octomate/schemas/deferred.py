from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, NotRequired, TypeAlias, TypedDict, cast

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
    JsonValue,
    TypeAdapter,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from uuid_utils.compat import uuid7

from octomate.models import deferred as deferred_models
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import (
    DeferredActionKind,
    DeferredActionStatus,
    DeferredApprovalStatus,
    DeferredBatchStatus,
    DeferredQuestionStatus,
)


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
    args: dict[str, JsonValue] = Field(default_factory=dict)
    title: str = "Permission Required"
    description: str = ""


@sqlalchemy_materia.bless(deferred_models.DeferredAction)
class DeferredAction(BaseTransmuter):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )

    id: Annotated[uuid.UUID, Identity] = Field(default_factory=uuid7, frozen=True)
    batch_id: uuid.UUID | None = None
    kind: DeferredActionKind
    status: DeferredActionStatus = "pending"
    tool_name: str
    tool_call_id: str
    position: int = 0
    args: dict[str, JsonValue]
    metadata: dict[str, JsonValue] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("meta", "metadata"),
    )
    result: JsonValue = None
    platform_message_id: str | None = None
    responder_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None

    batch: Relation[DeferredActionBatch] = Relationship()

    @property
    def resolved(self) -> bool:
        return self.status in {"answered", "approved", "denied", "expired", "failed"}


@sqlalchemy_materia.bless(deferred_models.DeferredQuestionAction)
class DeferredQuestion(DeferredAction):
    kind: Literal["question"] = "question"
    args: QuestionRequest
    status: DeferredQuestionStatus = "pending"
    result: str | None = None

    def __lt__(self, other: DeferredQuestion) -> bool:
        return self.position < other.position

    def __gt__(self, other: DeferredQuestion) -> bool:
        return self.position > other.position


@sqlalchemy_materia.bless(deferred_models.DeferredApprovalAction)
class DeferredApproval(DeferredAction):
    kind: Literal["approval"] = "approval"
    args: ApprovalRequest
    status: DeferredApprovalStatus = "pending"
    result: bool | None = None


DeferredActionVariant: TypeAlias = Annotated[
    DeferredQuestion | DeferredApproval,
    Field(discriminator="kind"),
]
DeferredActionVariantAdapter = TypeAdapter(DeferredActionVariant)


def from_deferred_requests(request: object) -> object:
    if not isinstance(request, DeferredToolRequests):
        return request
    action_payloads: list[object] = []
    for call in request.calls:
        metadata = request.metadata.get(call.tool_call_id, {})
        args = call.args_as_dict()
        questions = cast(list[QuestionRequest], args.get("questions") or [])
        if not questions:
            raise ValueError(f"deferred call {call.tool_call_id!r} has no questions")
        action_payloads.extend(
            {
                "kind": "question",
                "tool_name": call.tool_name,
                "tool_call_id": call.tool_call_id,
                "position": position,
                "args": question,
                "metadata": dict(metadata or {}),
            }
            for position, question in enumerate(questions)
        )
    action_payloads.extend(
        {
            "kind": "approval",
            "tool_name": call.tool_name,
            "tool_call_id": call.tool_call_id,
            "args": {
                "tool_name": call.tool_name,
                "args": call.args_as_dict(),
            },
            "metadata": dict(request.metadata.get(call.tool_call_id, {}) or {}),
        }
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
    run_name: str | None = "reception"
    status: DeferredBatchStatus = "pending"
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode = "main"
    decision: TriageDecision | None = None
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
