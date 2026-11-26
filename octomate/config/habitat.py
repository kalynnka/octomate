"""The service's habitat mode and the local sbx resource budget."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
)

DNS_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"


def validate_network_domain(value: str) -> str:
    authority = value.removeprefix("*.")
    url = HttpUrl(f"http://{authority}")
    host = url.host
    if host is None or len(host) > 253:
        raise ValueError("Host must contain between 1 and 253 characters")
    if value.startswith("*.") and (
        host == "localhost" or host.startswith("[") or host.replace(".", "").isdigit()
    ):
        raise ValueError(
            "Wildcards require a dotted domain name, not localhost or an IP address"
        )
    # URL parsing expands shorthand IPv4; network rules need the literal host they will match.
    if not host.startswith("[") and host != authority.split(":", 1)[0].lower():
        raise ValueError("Use a full dotted IPv4 address")
    return value


type NetworkDomainPattern = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            rf"^(?:\*\.)?(?:localhost|{DNS_LABEL}(?:\.{DNS_LABEL})+|"
            r"\[[0-9a-fA-F:.]+\])(?::[1-9][0-9]{0,4})?$"
        ),
    ),
    AfterValidator(validate_network_domain),
]


class BaseHabitatConfig(BaseModel):
    """The core API endpoint known to an isolated execution environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    core_address: HttpUrl = Field(
        default=HttpUrl("http://127.0.0.1:8000"),
        description="Core API origin (scheme, host and port), translated by the habitat for its network. Independent of core's listening host and port.",
    )
    builtin_sandbox: bool | None = Field(
        default=None,
        description="Whether agent harnesses apply their own OS sandbox. None enables it for native execution and disables it inside managed habitats. Independent of tool approval settings; support depends on the harness.",
    )


class NativeHabitatConfig(BaseHabitatConfig):
    """Each harness runs directly on the host, inside its own sandbox."""

    mode: Literal["native"] = "native"


class SrtHabitatConfig(BaseHabitatConfig):
    """Owner-scoped host processes confined by the service-installed SRT runtime."""

    mode: Literal["srt"] = "srt"
    executable: str = Field(
        default="srt",
        min_length=1,
        description="The srt entry point from the @anthropic-ai/sandbox-runtime npm installation; its adjacent SDK is loaded with Node.js.",
    )
    idle_timeout: float = Field(
        default=60,
        gt=0,
        allow_inf_nan=False,
        description="Seconds after the last invocation before releasing retained processes; storage persists.",
    )
    read_paths: list[Path] = Field(
        default_factory=list,
        description="Additional trusted runtime paths readable by sandboxed processes. Never grant the core checkout, operator home or credential stores wholesale.",
    )
    denied_domains: list[NetworkDomainPattern] = Field(
        default_factory=list,
        description="Blocked proxy destinations: ASCII hostnames, *.example.com patterns (all strict subdomain depths), localhost, IPv4 or bracketed IPv6, optionally restricted by :port (1-65535). Unlisted destinations are allowed through the proxy. Direct host loopback access stays disabled.",
    )


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
    denied_domains: list[NetworkDomainPattern] = Field(
        default_factory=list,
        description="Blocked outbound destinations, with optional *. subdomains (one label under sbx matching) or :port (1-65535). Unlisted destinations remain allowed subject to host and organization policy. Applied when creating the sandbox; changing this list requires recreating it.",
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
        default=300,
        gt=0,
        allow_inf_nan=False,
        description="Seconds after the last invocation finishes before stopping the user sandbox, preserving its disk.",
    )


type HabitatConfig = Annotated[
    NativeHabitatConfig | SrtHabitatConfig | SbxHabitatConfig,
    Field(discriminator="mode"),
]
