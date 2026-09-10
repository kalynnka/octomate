from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy import DateTime, Dialect, String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase): ...


type MapperArgs = Mapping[str, Any]


class UTCDateTime(TypeDecorator[datetime]):
    """Aware timestamps, normalized to UTC across database dialects.

    SQLite stores no timezone, so its existing naive values represent UTC. Restore
    that timezone on reads; new naive inputs have no known instant and are refused.
    """

    impl: DateTime = DateTime(timezone=True)
    cache_ok: bool = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("Datetime must include a timezone")
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class PathString(TypeDecorator[Path]):
    """A filesystem path, stored as text, where the empty string is no path at all.

    A path is a `Path` everywhere above this line. The driver binds only str, bytes,
    int, float and None, so the conversion belongs here rather than at every caller.

    `Path("")` is `Path(".")` — the process's own directory — so nothing stored reads
    back as None rather than as a path that looks real, and None is written `''`
    rather than NULL. Both columns using this are NOT NULL, and `''` is what
    `agent_runs.cwd` has always held for a run whose source reported no directory.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Path | None, dialect: Dialect) -> str:
        return "" if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Path | None:
        return Path(value) if value else None


class SecretString(TypeDecorator[SecretStr]):
    """A credential: `SecretStr` everywhere above this line, plain text in the
    column. The driver binds only primitives, so the unwrapping belongs here
    rather than at every caller — and a read re-wraps, so the value never sits
    unmasked on a mapped attribute."""

    impl = String
    cache_ok = True

    def process_bind_param(
        self, value: SecretStr | None, dialect: Dialect
    ) -> str | None:
        return None if value is None else value.get_secret_value()

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> SecretStr | None:
        return None if value is None else SecretStr(value)
