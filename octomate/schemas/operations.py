"""Thread operations exposed to authenticated clients."""

from pydantic import BaseModel, Field

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import AgentRoute


class OperationAvailability(BaseModel):
    """Whether one operation can run on a thread, and with what."""

    destinations: list[ChannelAddress] = Field(
        default_factory=list,
        description="Suggested places to go. An empty list sets no reason by "
        "itself: the destination browser can still find one.",
    )
    here: ChannelAddress | None = Field(
        default=None,
        description="For Summon, this conversation's own address, where it is "
        "handed over.",
    )
    routes: dict[str, list[AgentRoute]] = Field(
        default_factory=dict,
        description="What the operation can run, keyed by channel id: for Summon, "
        "every other agent on this conversation's own channel; for Teleport, the "
        "conversation's own on each connected channel, where a channel with none "
        "cannot be the destination.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the operation is unavailable; set only then.",
    )


class ThreadOperations(BaseModel):
    """What the console may do with a thread: each operation's availability, the
    thread's own surface, and the channels nothing can land in."""

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
