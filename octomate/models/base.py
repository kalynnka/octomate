from collections.abc import Mapping
from typing import Any, TypeAlias

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase): ...


MapperArgs: TypeAlias = Mapping[str, Any]
