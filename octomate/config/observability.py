"""Logging levels and Logfire export settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

LogLevel = Annotated[
    Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"],
    BeforeValidator(lambda level: level.upper() if isinstance(level, str) else level),
]


class LoggingConfig(BaseModel):
    """Console log levels: the root level and per-logger overrides."""

    level: LogLevel = "INFO"
    loggers: dict[str, LogLevel] = Field(
        default_factory=dict,
        description="Per-logger level overrides applied on top of `level` — e.g. "
        "`{httpx: WARNING}` to quiet a chatty library.",
    )


class LogfireInstrumentConfig(BaseModel):
    """Library auto-instrumentation toggles, all off by default: each one traces a
    whole library's internals (every HTTP request, every SQL statement, every model
    round-trip), which is diagnostic-session volume rather than always-on volume.
    Octomate's own spans — the `octomate.*`-scoped ones in `telemetry.py` — are not
    behind these: they always emit whenever tracing itself is on."""

    pydantic_ai: bool = Field(
        default=False,
        description="Trace pydantic-ai internals: model requests, tool calls, and "
        "token usage per agent run.",
    )
    httpx: bool = Field(
        default=False,
        description="Trace every outbound httpx request (LLM providers, Slack, "
        "Lark, NapCat — all of them).",
    )
    sqlalchemy: bool = Field(
        default=False,
        description="Trace every SQL statement the engine executes.",
    )


class LogfireConfig(BaseModel):
    """Logfire export: service identity, where spans go, and per-library
    instrumentation."""

    service_name: str = "octomate"
    environment: str = "development"
    send_to_logfire: bool = False
    base_url: str | None = Field(
        default=None,
        description="Ingest base URL override: a reverse proxy (a Cloudflare "
        "Worker, say) that forwards to the real logfire region host, for "
        "networks where logfire's own domains are unreachable. "
        "`generate_base_url` honours it over the token's region, so the served "
        "OTLP environment follows it too.",
    )
    console: bool = False
    scrub: bool = True
    instrument: LogfireInstrumentConfig = Field(
        default_factory=LogfireInstrumentConfig,
        description="Per-library auto-instrumentation; see LogfireInstrumentConfig.",
    )
