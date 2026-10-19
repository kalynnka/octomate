"""Configuration for exposing native agent sessions through ACP."""

from typing import Literal

from pydantic import BaseModel, Field


class AcpConfig(BaseModel):
    """Opt in to one ACP agent tentacle and its API.

    Declare once per deployment: the API paths are fixed, while the tentacle's
    registry key is chosen by the operator. Claude and Codex are provider choices
    within its connections and do not register additional tentacles.
    """

    type: Literal["acp"] = "acp"
    enabled: bool = Field(
        default=True,
        description="Connect the ACP tentacle and mount its API routes at startup.",
    )
