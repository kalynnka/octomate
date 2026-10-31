"""Agent sandbox selection and the local sbx resource budget."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class NativeSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["native"] = "native"


class SbxSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["sbx"] = "sbx"
    executable: str = Field(default="sbx", min_length=1)
    version: Literal["0.48.0"] = "0.48.0"
    image: str = Field(
        pattern=r"^[^\s]+@sha256:[0-9a-f]{64}$",
        description="Immutable worker image reference, matching the host architecture.",
    )
    cpus: int = Field(default=2, ge=1, strict=True)
    memory_mib: int = Field(default=2048, ge=512, strict=True)
    idle_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)
    max_active: int = Field(default=4, ge=1, strict=True)


type SandboxConfig = Annotated[
    NativeSandboxConfig | SbxSandboxConfig, Field(discriminator="mode")
]
