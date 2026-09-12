from pydantic import BaseModel, Field

from octomate.config.agents import AgentRouteModelName
from octomate.schemas.triage import AgentRoute


class AgentInfo(BaseModel):
    id: str = Field(description="The registered agent tentacle's id.")
    description: str = Field(description="The agent's own capability blurb.")
    gateway: bool = Field(
        description="This agent's half of the gateway switch — whether its driven "
        "turns offer the routing and project spells. The channel connection holds "
        "the other half, so this alone does not mean they are offered."
    )
    default_model: AgentRouteModelName | None = Field(
        description="What a directive naming no model runs on; null for an agent "
        "whose catalog is empty."
    )
    routes: list[AgentRoute]
    driven_sessions: int = Field(
        description="Live runs this instance is driving on the agent's runtime."
    )
    native_sessions: int = Field(
        description="Native transcript streams currently attached to this agent — "
        "sessions someone else started that this instance is reading, not driving."
    )
