from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

LogLevel = Annotated[
    Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"],
    BeforeValidator(lambda level: level.upper() if isinstance(level, str) else level),
]


class LoggingConfig(BaseModel):
    level: LogLevel = "INFO"
    loggers: dict[str, LogLevel] = Field(
        default_factory=dict,
        description="Per-logger level overrides applied on top of `level` — e.g. "
        "`{httpx: WARNING}` to quiet a chatty library.",
    )


class LogfireConfig(BaseModel):
    service_name: str = "octomate"
    environment: str = "development"
    send_to_logfire: bool = False
    console: bool = False
    scrub: bool = True
