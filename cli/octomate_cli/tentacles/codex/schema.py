"""Hook settings edited by the Codex installer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, Self

import typer
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    ValidationError,
)


class HookConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class OtherHook(HookConfigModel):
    """A runtime handler the installer preserves without interpreting."""

    type: str


class CommandHook(HookConfigModel):
    type: Literal["command"]
    command: str
    timeout: int | float | None = None


class HttpHook(HookConfigModel):
    type: Literal["http"]
    url: str
    timeout: int | float | None = None


def hook_kind(value: HookConfigModel | JsonValue) -> str | None:
    """Known handler types must validate; newer runtime types remain untouched."""
    if isinstance(value, CommandHook | HttpHook | OtherHook):
        kind = value.type
    elif isinstance(value, dict):
        kind = value.get("type")
    else:
        return None
    if not isinstance(kind, str):
        return None
    return kind if kind in {"command", "http"} else "other"


type Hook = Annotated[
    Annotated[CommandHook, Tag("command")]
    | Annotated[HttpHook, Tag("http")]
    | Annotated[OtherHook, Tag("other")],
    Discriminator(hook_kind),
]


class HookGroup(HookConfigModel):
    hooks: list[Hook]


class HookSettings(HookConfigModel):
    hooks: dict[str, list[HookGroup]] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Self:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(hooks={})
        if not raw.strip():
            return cls(hooks={})
        try:
            return cls.model_validate_json(raw)
        except ValidationError as error:
            raise typer.BadParameter(
                f"Invalid hook settings in {path}: {error}"
            ) from error

    def remove_handlers(self, event: str, matches: Callable[[Hook], bool]) -> None:
        kept = []
        for group in self.hooks.get(event, []):
            group.hooks = [hook for hook in group.hooks if not matches(hook)]
            if group.hooks:
                kept.append(group)
        if kept:
            self.hooks[event] = kept
        else:
            self.hooks.pop(event, None)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(
                indent=2,
                exclude_unset=True,
                exclude={"hooks"} if not self.hooks else None,
            )
            + "\n"
        )
