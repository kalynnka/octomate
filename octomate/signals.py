"""What core asks of an agent tentacle over the nerve.

A signal travels inbound, as a stimulus, to the tentacle that is to act on it.
The signals a kick delivers are passed on as they are, with the priming core
resolved for the turn beside them. Nothing is answered: what the tentacle does
shows in the run events it publishes back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import UUID7, BaseModel, ConfigDict, Field

from octomate.schemas.awakes import AwakeSignal
from octomate.types.json import JsonObject
from octomate.types.permissions import AgentPermissionMode


class Priming(BaseModel):
    """What core resolved for a turn before asking for it: the session it runs
    in, where, and under which settings. It rides beside the signal, on the
    stimulus that carries it. What holds for as long as the tentacle's habitat
    does, such as its environment, is the tentacle's own and is not sent."""

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
    invocation_id: UUID7


type Signal = Annotated[
    AwakeSignal | InterruptRunSignal, Field(discriminator="event_kind")
]
