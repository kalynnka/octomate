"""Sandbox lifecycle observations and signals exchanged with the core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import UUID7, AwareDatetime, BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.usage import RunUsage

from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.schemas.user import UserProfile
from octomate.types.json import JsonObject, JsonValue


class SandboxObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID7
    sandbox: str
    sequence: int = Field(ge=0, description="Monotonic within this manager lifetime.")
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    invocation_id: UUID7 | None = None
    thread_id: UUID7 | None = None


class SandboxProgress(SandboxObservation):
    event_kind: Literal["sandbox_progress"] = "sandbox_progress"
    phase: Literal["unknown", "preparing", "starting", "ready", "stopping", "stopped"]


class SandboxFailed(SandboxObservation):
    event_kind: Literal["sandbox_failed"] = "sandbox_failed"
    phase: Literal["failed"] = "failed"
    message: str = Field(
        description="Operator-actionable text without guest diagnostics."
    )


type SandboxSnapshot = Annotated[
    SandboxProgress | SandboxFailed, Field(discriminator="event_kind")
]


class InvocationScope(BaseModel):
    """Core-established ownership, never derived from an incoming sandbox signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID7
    mode: Literal["native", "sbx"]
    conversation_id: UUID7
    thread_id: UUID7 | None = None

    @classmethod
    def from_profile(
        cls,
        profile: UserProfile,
        *,
        mode: Literal["native", "sbx"],
        conversation_id: UUID7,
        thread_id: UUID7 | None = None,
    ) -> Self:
        """Accept only a profile already resolved by the core user registry."""
        if profile.user_id is None:
            raise ValueError("Sandbox access requires a registered user")
        return cls(
            user_id=profile.user_id,
            mode=mode,
            conversation_id=conversation_id,
            thread_id=thread_id,
        )

    def require_binding(self, bound: InvocationScope) -> None:
        """Refuse another owner's or sandbox mode's private conversation state."""
        if self != bound:
            raise ValueError(
                "Invocation owner, sandbox mode or conversation does not match"
            )


class SandboxAgentSignal(BaseModel):
    model_config = ConfigDict(
        extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )


class SandboxAgentReady(SandboxAgentSignal):
    kind: Literal["ready"] = "ready"
    user_id: UUID7
    image: str
    cpus: int = Field(ge=1)
    memory_mib: int = Field(ge=512)
    accepts_invocations: bool = Field(
        description="False for the bootstrap sandbox agent, which rejects agent invocations."
    )


class InvocationMessage(SandboxAgentSignal):
    invocation_id: UUID7


class ExecuteAgent(InvocationMessage):
    kind: Literal["execute"] = "execute"
    scope: InvocationScope
    agent_id: str
    model: str | None = None
    effort: str | None = None
    prompt: str | list[UserContent] | None = None
    history: list[ModelMessage] = Field(default_factory=list)
    deferred_results: DeferredToolResults | None = None
    address: ChannelAddress
    source_address: ChannelAddress | None = None
    source_message_ids: list[UUID7] = Field(default_factory=list)
    run_name: str
    workspace: str = Field(description="Guest path in sbx mode; never a host mount.")


class ProvideInput(InvocationMessage):
    kind: Literal["input"] = "input"
    response: DeferredActionBatchResponse


class CancelInvocation(InvocationMessage):
    kind: Literal["cancel"] = "cancel"


class InvocationAccepted(InvocationMessage):
    kind: Literal["accepted"] = "accepted"


class InvocationRejected(InvocationMessage):
    kind: Literal["rejected"] = "rejected"
    reason: str


class InvocationResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    run_id: UUID7
    output: str | list[MessageSegment] | DeferredToolRequests | None
    messages: list[ModelMessage]
    usage: RunUsage
    external_session_id: str | None = None
    artifact_ids: list[UUID7] = Field(default_factory=list)


class InvocationCompleted(InvocationMessage):
    kind: Literal["completed"] = "completed"
    result: InvocationResult


class InvocationFailed(InvocationMessage):
    kind: Literal["failed"] = "failed"
    reason: str


class InvocationCancelled(InvocationMessage):
    kind: Literal["cancelled"] = "cancelled"
    terminated: bool = Field(
        description="True only after guest termination is verified."
    )


class ReadConversation(BaseModel):
    operation: Literal["read_conversation"] = "read_conversation"
    conversation_id: UUID7


class RecordRun(BaseModel):
    operation: Literal["record_run"] = "record_run"
    result: InvocationResult


class GatewayCall(BaseModel):
    operation: Literal["gateway"] = "gateway"
    tool: Literal[
        "inspect", "summon", "teleport", "scheme", "dismiss", "commission", "whisper"
    ]
    arguments: JsonObject = Field(
        description="Validated by the named gateway tool before authorization/execution."
    )


class ServiceRequest(InvocationMessage):
    kind: Literal["service_request"] = "service_request"
    request_id: UUID7
    request: Annotated[
        ReadConversation | RecordRun | GatewayCall,
        Field(discriminator="operation"),
    ]


class ServiceReply(InvocationMessage):
    kind: Literal["service_reply"] = "service_reply"
    request_id: UUID7
    result: JsonValue


class ServiceRejected(InvocationMessage):
    kind: Literal["service_rejected"] = "service_rejected"
    request_id: UUID7
    reason: str


type SandboxSignal = Annotated[
    ExecuteAgent
    | ProvideInput
    | CancelInvocation
    | ServiceReply
    | ServiceRejected
    | SandboxAgentReady
    | InvocationAccepted
    | InvocationRejected
    | InvocationCompleted
    | InvocationFailed
    | InvocationCancelled
    | ServiceRequest,
    Field(discriminator="kind"),
]
