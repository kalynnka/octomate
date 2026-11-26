"""Resolved work and controls sent by core to agent tentacles over the nerve.

Core routes and prepares a turn before sending its signal. The agent publishes
execution events outbound; it does not receive the request that woke core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import UUID7, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from octomate.schemas.conversation import ChannelAddress
from octomate.types.json import JsonObject
from octomate.types.permissions import AgentPermissionMode


class Priming(BaseModel):
    """What core resolved for a turn before asking for it: the session it runs
    in, where, and under which settings. The run signal carries it. What holds
    for as long as the tentacle's habitat does, such as its environment, is the
    tentacle's own and is not sent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: UUID7 = Field(
        description="Allocated by core; names the turn to `InterruptRunSignal`."
    )
    session_id: str = Field(description="The harness session the turn runs in.")
    resume: bool = Field(
        description="Whether the harness already holds that session. A session "
        "it does not hold is opened under the given id."
    )
    model: str | None = Field(
        default=None, description="None leaves the harness's own choice."
    )
    effort: str | None = Field(
        default=None, description="None leaves the harness's own choice."
    )
    permission_mode: AgentPermissionMode | None = Field(
        default=None,
        description="In the harness's own vocabulary; None leaves the mode the "
        "tentacle is configured with.",
    )
    instructions: str | None = Field(
        default=None,
        description="Appended to the harness's system prompt for this turn.",
    )
    output_schema: JsonObject | None = Field(
        default=None,
        description="The JSON schema the turn's output must satisfy; None for a "
        "plain text reply.",
    )
    interactive: bool = Field(
        default=True,
        description="Whether a human can be asked. A turn without one declines "
        "what would need an approval or an answer.",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Tools already approved for this session, which run unasked.",
    )
    cwd: Path = Field(
        description="The working directory, as the harness's machine shows it."
    )
    extra_roots: list[Path] = Field(
        default_factory=list,
        description="Other directories the turn may reach, on the same machine.",
    )


class InterruptRunSignal(BaseModel):
    """Asks an agent tentacle to stop a turn it is running."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_kind: Literal["interrupt_run_signal"] = "interrupt_run_signal"
    invocation_id: UUID7 = Field(
        description="Core's dispatch identity, not the provider run or session ID."
    )


class RunAgentSignal(BaseModel):
    """Execute core's resolved turn with its required priming.

    Core retains the invocation's resource context until execution settles. The
    receiver resolves references through its owners; no live resource crosses
    this boundary. Conversation ownership also selects any child agent context.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_kind: Literal["run_agent_signal"] = "run_agent_signal"
    priming: Priming = Field(
        description="Core-resolved invocation identity, session and execution settings."
    )
    conversation_id: UUID7 = Field(
        description="Core-resolved conversation, including a child's own conversation; "
        "its owner supplies thread, agent, parent and subagent identities."
    )
    user_id: UUID7 | None = Field(
        description="Authorized registered user whose capabilities the receiver resolves; "
        "None preserves an unregistered caller."
    )
    conversation_address: ChannelAddress = Field(
        description="Destination resolved by core before dispatch."
    )
    source_thread_address: ChannelAddress = Field(
        description="Origin retained for source-message and attachment resolution."
    )
    source_thread_message_ids: list[UUID7] = Field(
        description="Core ledger messages the receiver may resolve for this turn."
    )
    run_name: str = Field(
        description="Core's run label, also used for commissioned runs."
    )
    user_prompt: str | None = Field(
        default=None,
        description="Resolved opening text; None continues deferred tool calls.",
    )
    deferred_batch_id: UUID7 | None = Field(
        default=None,
        description="Completed batch whose owner rebuilds deferred results for a new invocation.",
    )
    tool_results: dict[str, str] | None = Field(
        default=None,
        min_length=1,
        description="Graph-generated answers keyed by tool call ID, such as a completed "
        "teleport; the receiver builds SDK results without requiring a human-action batch.",
    )

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        inputs = (self.user_prompt, self.deferred_batch_id, self.tool_results)
        if sum(value is not None for value in inputs) != 1:
            raise ValueError(
                "a run requires exactly one prompt, deferred batch or tool result input"
            )
        return self


class DeferredResponseSignal(BaseModel):
    """Release a live invocation's deferred wait using an already resolved batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_kind: Literal["deferred_response_signal"] = "deferred_response_signal"
    invocation_id: UUID7 = Field(
        description="Core's identity of the still-live invocation."
    )
    batch_id: UUID7 = Field(
        description="Persisted completed batch; its owner supplies the validated answers."
    )


type Signal = Annotated[
    RunAgentSignal | DeferredResponseSignal | InterruptRunSignal,
    Field(discriminator="event_kind"),
]

signal_adapter: TypeAdapter[Signal] = TypeAdapter(Signal)
