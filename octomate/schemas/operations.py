"""Thread operations exposed to authenticated clients."""

from pydantic import BaseModel, Field

from octomate.schemas.triage import AgentRoute, SummonTarget


class OperationDestination(BaseModel):
    target: SummonTarget
    label: str
    routes: list[AgentRoute] = Field(default_factory=list)


class OperationAvailability(BaseModel):
    destinations: list[OperationDestination] = Field(default_factory=list)
    reason: str | None = None


class ThreadOperations(BaseModel):
    teleport: OperationAvailability
    summon: OperationAvailability
