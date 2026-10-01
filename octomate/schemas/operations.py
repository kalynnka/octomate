"""Thread operations exposed to authenticated clients."""

from pydantic import BaseModel, Field

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import AgentRoute


class OperationAvailability(BaseModel):
    destinations: list[ChannelAddress] = Field(default_factory=list)
    here: ChannelAddress | None = None
    routes: dict[str, list[AgentRoute]] = Field(default_factory=dict)
    reason: str | None = None


class ThreadOperations(BaseModel):
    teleport: OperationAvailability
    summon: OperationAvailability
