"""The service's habitat mode and the local sbx resource budget."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class BaseHabitatConfig(BaseModel):
    """The core API endpoint known to an isolated execution environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    core_address: HttpUrl = Field(
        default=HttpUrl("http://127.0.0.1:8000"),
        description="Core API origin (scheme, host and port), translated by the habitat for its network. Independent of core's listening host and port.",
    )


class NativeHabitatConfig(BaseHabitatConfig):
    """Each harness runs directly on the host, inside its own sandbox."""

    mode: Literal["native"] = "native"


class SbxHabitatConfig(BaseHabitatConfig):
    """Each registered user's agents run in one persistent sbx VM."""

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


type HabitatConfig = Annotated[
    NativeHabitatConfig | SbxHabitatConfig, Field(discriminator="mode")
]
