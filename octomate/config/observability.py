from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"]


class LoggingConfig(BaseModel):
    level: LogLevel = "INFO"
    format: str = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    @field_validator("level", mode="before")
    @classmethod
    def normalize_level(cls, value: str | None) -> str | None:
        return value.upper() if isinstance(value, str) else value


class LogfireConfig(BaseModel):
    service_name: str = "octomate"
    environment: str = "development"
    send_to_logfire: bool = False
    console: bool = False
    scrub: bool = True
