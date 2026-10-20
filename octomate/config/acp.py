"""Configuration for exposing Octomate through ACP."""

from typing import Literal

from pydantic import BaseModel, Field


class AcpConfig(BaseModel):
    """Opt in to one ACP agent tentacle and its API.

    Declare once per deployment: the API paths are fixed, while the tentacle's
    registry key is chosen by the operator. The endpoint exposes the model
    catalogs of enabled agent tentacles as standard ACP configuration options.
    """

    type: Literal["acp"] = "acp"
    enabled: bool = Field(
        default=True,
        description="Connect the ACP tentacle and mount its API routes at startup.",
    )
