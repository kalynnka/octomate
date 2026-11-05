"""Agent sandbox selection and the local sbx resource budget."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SbxSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Deployment ID referenced by agent tentacles; scopes each user's sandbox.",
    )
    mode: Literal["sbx"] = "sbx"
    executable: str = Field(default="sbx", min_length=1)
    version: Literal["0.48.0"] = "0.48.0"
    image: str = Field(
        pattern=r"^[^\s]+@sha256:[0-9a-f]{64}$",
        description="Immutable sandbox agent image reference, matching the host architecture.",
    )
    cpus: int = Field(
        default=2, ge=1, strict=True, description="Virtual CPU count per sandbox."
    )
    memory_mib: int = Field(
        default=2048,
        ge=512,
        strict=True,
        description="Guest RAM size in MiB, passed to sbx --memory.",
    )
    idle_timeout: float = Field(
        default=900,
        gt=0,
        allow_inf_nan=False,
        description="Seconds after the last invocation finishes before stopping the user sandbox, preserving its disk.",
    )


type SandboxConfig = SbxSandboxConfig
