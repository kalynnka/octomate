"""Gateway decisions a run leaves."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Annotated, Literal, NamedTuple

from octomate_protocol.gateway import GatewayTool
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.settings import ThinkingEffort
from pydantic_ai.tools import DeferredToolRequests

from octomate.config.agents import AgentRouteModelName, Claim
from octomate.schemas.conversation import ChannelAddress

ResponseTargetMode = Literal["main", "sub"]
# How the react loop was entered, and thus what to call the agent run it drives:
# `react` (an initial reaction to an inbound message), `summon` (a handoff to another
# agent), `teleport` (the same agent resuming in a forked sub-thread), or `resume`
# (continuing after human review). Labels each run's span and any batch it defers.
RunName = Literal["react", "summon", "teleport", "resume"]

# What one `inspect` reveals. One facet per call, because each spell needs exactly one —
# a route for `summon`, a place for anything that lands somewhere, a project for
# `teleport` — and the routes alone run long enough that showing everything every time
# buried the line the caller came for. A tool result is the only place a per-user
# list can reach the model without forking a cached prompt segment.
InspectFacet = Literal["routes", "destinations", "projects"]
# The `teleport` deferral's declared metadata kind. The suspender and dispatch graph
# classify the deferral by this kind rather than the tool name, so the gateway (which
# emits it) and `reflex` (which resolves it) agree on one value without matching on
# the name.
TELEPORT_DEFER_KIND = "teleport"


class AgentRouteKey(NamedTuple):
    """An agent and a model, or None for the agent's native default."""

    agent_id: str
    model: AgentRouteModelName | None  # None preserves the harness's native default.


class SpellTarget(BaseModel):
    """Base of the places a spell's `destination` argument can name.

    A variant per place rather than one free string, so a tool definition says what
    it accepts instead of documenting it in prose. Channel IDs are runtime values:
    the schema stays stable while the tool validates reachability and access.
    """

    model_config = ConfigDict(frozen=True)


class HereTarget(SpellTarget):
    """This conversation, as it stands."""

    kind: Literal["here"] = "here"


class DirectTarget(SpellTarget):
    """The asking user's direct messages on a connected channel."""

    kind: Literal["dm"] = "dm"

    channel: str | None = Field(
        default=None,
        description="The connected channel ID; omit for this conversation's channel.",
    )


type SchemeTarget = DirectTarget
type SendTarget = Annotated[HereTarget | DirectTarget, Field(discriminator="kind")]

HERE_TARGET = HereTarget()
DIRECT_TARGET = DirectTarget()


class SummonDecision(BaseModel):
    """A handoff decision: continue this turn with another agent, from a brief."""

    action: Literal["summon"] = "summon"
    reason: str
    agent_id: str
    model: AgentRouteModelName | None = Field(
        description="Selected model, or null to use the harness's native default."
    )
    destination: ChannelAddress | None = Field(
        default=None,
        description="The address to use; None means the current conversation.",
    )
    new_thread: bool = Field(
        default=True, description="Create a thread at the address before handing over."
    )
    effort: ThinkingEffort | None = None
    hint: str
    summon: str = Field(
        max_length=8_000,
        description="The receiver's whole opening prompt, refused over the cap rather "
        "than trimmed: what it leaves out, the receiver reads back through the "
        "handoff. Eight thousand characters is roughly two thousand tokens — the "
        "size a distilled handoff runs to, and well short of a pasted transcript.",
    )

    @property
    def key(self) -> AgentRouteKey:
        return AgentRouteKey(agent_id=self.agent_id, model=self.model)


class SchemeDecision(BaseModel):
    """Take this turn to the asking user's DM, from a brief.

    No agent is named: whoever already handles that user's DM picks the work up, so a
    group can never point someone's private assistant at an agent it chose. The
    receiver is resolved against the DM's own thread, which is why this carries a brief
    rather than a route.
    """

    action: Literal["scheme"] = "scheme"
    hint: str = Field(
        description="The line that opens the conversation over there. Nothing is "
        "posted where the request came from — the run's own reply closes that out."
    )
    brief: str = Field(max_length=8_000)
    destination: ChannelAddress = Field(
        description="Which direct messages, resolved by the gateway using the "
        "requesting user's linked identity, never a model-supplied user ID."
    )


class TeleportDecision(BaseModel):
    """The same agent continues somewhere else, its history with it; Reflex
    performs the move. A run ends on it — Inkling by deferring the call, a runtime
    a tool result cannot suspend by being interrupted on the recorded decision and
    ending its turn as the same deferral — and the graph resumes the agent in the
    new place: a sub-thread, a crossing, or this very thread when the move is only
    into a project's workspace.
    """

    action: Literal["teleport"] = "teleport"
    agent_id: str = Field(
        description="The driven agent selected to resume the history at the destination."
    )
    hint: str = Field(description="The short, user-facing thread-starter message.")
    destination: ChannelAddress | None = Field(
        default=None,
        description="The address to use; None means the current conversation.",
    )
    new_thread: bool = Field(
        default=True,
        description="Create a thread at the address; false only binds the current thread to a project.",
    )
    project: str | None = Field(
        default=None,
        description="The project the thread landed in is bound to, and whose "
        "workspace the agent resumes in; None carries the conversation only.",
    )
    ref: str | None = Field(
        default=None,
        description="The branch, tag or commit that workspace starts from; None "
        "for the project's default branch.",
    )

    def metadata(self) -> dict[str, str | bool]:
        """What the deferral carries, as plain values: enough for the graph to
        rebuild this decision at its boundary."""
        return {
            "kind": TELEPORT_DEFER_KIND,
            "hint": self.hint,
            "destination": TypeAdapter(ChannelAddress)
            .dump_json(self.destination)
            .decode()
            if self.destination is not None
            else "",
            "new_thread": self.new_thread,
            "project": self.project or "",
            "ref": self.ref or "",
        }

    def deferral(self, tool_call_id: str) -> DeferredToolRequests:
        """This move as the deferral the graph performs — what an interrupted
        runtime's turn ends with, shaped as Inkling's own `CallDeferred` is."""
        return DeferredToolRequests(
            calls=[
                ToolCallPart(
                    tool_name=GatewayTool.TELEPORT,
                    args={
                        "hint": self.hint,
                        "destination": asdict(self.destination)
                        if self.destination is not None
                        else None,
                        "new_thread": self.new_thread,
                        "project": self.project,
                        "ref": self.ref,
                    },
                    tool_call_id=tool_call_id,
                )
            ],
            metadata={tool_call_id: self.metadata()},
        )


# Every decision a gateway can record for the graph to act on after the turn.
type GatewayDecision = Annotated[
    SummonDecision | SchemeDecision | TeleportDecision, Field(discriminator="action")
]


@dataclass(frozen=True)
class AgentRoute:
    """A summonable (agent, model) pair and the claim it advertises. Agents
    advertise; the caller requests — the claim publishes the space this route
    supports, and a caller picks a point in it."""

    agent_id: str
    model: AgentRouteModelName
    claim: Claim

    @property
    def key(self) -> AgentRouteKey:
        return AgentRouteKey(agent_id=self.agent_id, model=self.model)

    def __str__(self) -> str:
        return f"- agent_id={self.agent_id}, model={self.model!r}: {self.claim}"


@dataclass(frozen=True)
class ProjectSummary:
    """One registered project, as a model choosing between them needs it."""

    # Deliberately not the root: which absolute path a project is on the server is
    # the operator's business, and naming it in a chat thread is how it leaks.
    name: str
    description: str | None

    def __str__(self) -> str:
        if self.description is None:
            return f"- {self.name}"
        return f"- {self.name}: {self.description}"
