from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Generic, Self, TypeVar

from fastapi import APIRouter
from rich.style import Style

if TYPE_CHECKING:
    from octomate.base import Octomate

TentaclePrimaryT = TypeVar("TentaclePrimaryT")
TentacleSecondaryT = TypeVar("TentacleSecondaryT")


@dataclass
class Tentacle(Generic[TentaclePrimaryT, TentacleSecondaryT]):
    """Base for external integrations managed by the Octomate host.

    A tentacle is a lifecycle component, not a message dispatcher. Channel
    tentacles receive platform events and call Octomate.awake; agent tentacles
    expose pydantic-ai-style run methods.

    A tentacle is an async context manager: the host enters it to start the
    tentacle's long-lived resources and exits it to tear them down. The base
    implementation is a no-op; subclasses override `__aenter__`/`__aexit__`.
    """

    id: str
    octomate: Octomate = field(repr=False)
    # The console log color for this tentacle's lines, dispatched by the Octomate
    # host when the tentacle is connected (see `Octomate.connect`).
    log_color: Style | None = field(default=None, init=False, repr=False)
    # A tentacle fronting something with a color of its own claims it here, and the
    # host dispatches that instead of generating a hue from the connection order.
    brand_color: ClassVar[Style | None] = None

    @property
    def log_names(self) -> tuple[str, ...]:
        """Logger-name prefixes whose records belong to this tentacle — its own
        module tree by default. Channels with a chatty SDK extend this to claim
        the SDK's loggers too, so those lines share the tentacle's color."""
        return (type(self).__module__.rsplit(".", 1)[0],)

    def routers(self) -> tuple[APIRouter, ...]:
        """FastAPI routers the host mounts when this tentacle is connected — empty by
        default. A tentacle that exposes an HTTP surface (e.g. an inbound hook
        endpoint) overrides this. Mounting happens at connect time, off the tentacle's
        async lifecycle: the routes are owned by the Octomate app, not the tentacle."""
        return ()

    async def __aenter__(self) -> Self:
        """Start any long-lived platform resources owned by the tentacle."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop any long-lived platform resources owned by the tentacle."""


# Level colors follow the usual severity convention — fixed, not per-tentacle.
LEVEL_STYLES: dict[int, Style] = {
    logging.DEBUG: Style(dim=True),
    logging.INFO: Style(color="green"),
    logging.WARNING: Style(color="yellow"),
    logging.ERROR: Style(color="red"),
    logging.CRITICAL: Style(color="red", bold=True),
}


class TentacleLogFormatter(logging.Formatter):
    """Renders `LEVEL     timestamp [logger] message` — the level tinted by
    severity and the `[logger]` header tinted with the color the host dispatched
    to the owning tentacle. `colorize` is off for non-TTY streams so redirected
    logs stay plain. Lives with the tentacle base since it renders tentacle
    identity; the host resolves each logger to its tentacle via `log_tag`."""

    def __init__(self, host: Octomate, *, colorize: bool) -> None:
        super().__init__()
        self.host = host
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        tag, style = self.host.log_tag(record.name)
        level = f"{record.levelname:<8}"
        header = f"[{tag}]"
        if self.colorize:
            level = LEVEL_STYLES.get(record.levelno, Style()).render(level)
            if style is not None:
                header = style.render(header)
        line = f"{level} {self.formatTime(record)} {header} {record.getMessage()}"
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line = f"{line}\n{record.exc_text}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line
