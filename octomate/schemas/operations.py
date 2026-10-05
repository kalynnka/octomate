"""Thread operations exposed to authenticated clients."""

from pydantic import BaseModel, Field

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import AgentRoute


class OperationAvailability(BaseModel):
    destinations: list[ChannelAddress] = Field(default_factory=list)
    here: ChannelAddress | None = None
    routes: dict[str, list[AgentRoute]] = Field(
        default_factory=dict,
        description="What the operation can run, keyed by channel id: for Summon, "
        "every other agent on this conversation's own channel; for Teleport, the "
        "conversation's own on each connected channel, where a channel with none "
        "cannot be the destination.",
    )
    reason: str | None = None


class ThreadOperations(BaseModel):
    source: ChannelAddress | None = Field(
        description="The surface this conversation is on. Its `shared`, against a "
        "destination's, is what a client warns by before private history lands in "
        "a shared place."
    )
    teleport: OperationAvailability
    summon: OperationAvailability
    barred: dict[str, str] = Field(
        default_factory=dict,
        description="Connected channels where no conversation can land, keyed by "
        "channel id, with why: shown to either operation, never picked.",
    )
