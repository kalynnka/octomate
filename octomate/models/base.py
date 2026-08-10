from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeAlias

from sqlalchemy import Dialect, String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase): ...


MapperArgs: TypeAlias = Mapping[str, Any]


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
