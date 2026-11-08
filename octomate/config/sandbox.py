"""The service's execution mode and the local sbx resource budget."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class NativeSandboxConfig(BaseModel):
    """Each harness runs directly on the host, inside its own sandbox."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["native"] = "native"


class SbxSandboxConfig(BaseModel):
    """Each registered user's agents run in one persistent sbx VM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["sbx"] = "sbx"
    executable: str = Field(default="sbx", min_length=1)
    version: Literal["0.48.0"] = "0.48.0"
    image: str = Field(
        pattern=r"^[^\s]+@sha256:[0-9a-f]{64}$",
        description="Immutable sandbox agent image reference, matching the host architecture.",
    )
    internet_access: bool = Field(
        default=True,
        strict=True,
        description="Allow outbound TCP through the sbx proxy, subject to host and organization policy; false explicitly denies outbound TCP. Set when creating the sandbox.",
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


type SandboxConfig = Annotated[
    NativeSandboxConfig | SbxSandboxConfig, Field(discriminator="mode")
]
