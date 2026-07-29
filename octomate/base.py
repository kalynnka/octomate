from __future__ import annotations

import asyncio
import colorsys
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import InitVar, dataclass, field
from typing import TypeVar

from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import SecretStr
from rich.color import Color
from rich.style import Style

from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.oauth import OAuthManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.reflex import (
    Awake,
    ReflexDeps,
    ReflexState,
    reflex_graph,
)
from octomate.schemas.awakes import (
    AwakeSignal,
    DeferredActionBatchResponse,
    UserMessageSignal,
)
from octomate.schemas.base import sqlalchemy_materia
from octomate.telemetry import octomate_logfire
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel.base import ChannelTentacle

TentacleT = TypeVar("TentacleT", bound=Tentacle)
logger = logging.getLogger(__name__)

# Backstop for a tentacle whose startup hangs (a WebSocket connect that never
# completes, an unbounded probe). Past this, the host gives up on that tentacle
# and serves the rest rather than never finishing startup.
TENTACLE_START_TIMEOUT = 30.0

GOLDEN_RATIO_CONJUGATE = 0.618033988749895


def log_style_for_index(index: int) -> Style:
    """A distinct, evenly-spread console color per connection index — golden-ratio
    hue stepping keeps successive tentacles far apart on the color wheel without a
    fixed palette."""
    hue = (index * GOLDEN_RATIO_CONJUGATE) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.6, 1.0)
    return Style(color=Color.from_rgb(red * 255, green * 255, blue * 255), bold=True)


def short_log_name(name: str) -> str:
    """Trim our package prefix to the subsystem so shared logger tags stay short —
    `octomate.reflex.graph` -> `reflex`, `octomate.tentacles.channel.base` ->
    `channel`. Library loggers are left as-is."""
    for prefix in ("octomate.tentacles.", "octomate."):
        if name.startswith(prefix):
            return name[len(prefix) :].split(".")[0]
    return name


# Shared subsystems front no tentacle, so they have neither a brand to claim nor a
# connection index to step the wheel with. The host's own voice and the plumbing every
# channel shares take a neutral color here, so they read as the structure around the
# tentacles rather than as another hue competing with them.
SHARED_LOG_STYLES: dict[str, Style] = {
    "main": Style(color="bright_white", bold=True),
    "channel": Style(color="grey62", bold=True),
}


@dataclass
class Octomate:
    """Application host for shared services, agents, channels, and routers."""

    thread_manager: ThreadManager = field(init=False)
    conversations: ConversationManager = field(default_factory=ConversationManager)
    deferred_actions: DeferredActionManager = field(
        default_factory=DeferredActionManager
    )
    users: UserManager = field(default_factory=UserManager)
    oauth_encryption_key: InitVar[SecretStr | None] = None
    oauth: OAuthManager = field(init=False)
    agents: dict[str, AgentTentacle] = field(default_factory=dict)
    channels: dict[str, ChannelTentacle] = field(default_factory=dict)
    routers: list[APIRouter] = field(default_factory=list)

    def __post_init__(self, oauth_encryption_key: SecretStr | None) -> None:
        # Every ledger row references its sender's registry profile, so the
        # thread manager records through the host's one identity registry.
        self.thread_manager = ThreadManager(users=self.users)
        self.oauth = OAuthManager(
            users=self.users,
            encryption_key=oauth_encryption_key,
        )

    def connect(self, tentacle: TentacleT) -> TentacleT:
        if isinstance(tentacle, ChannelTentacle):
            tentacle.octomate = self
            if tentacle.id in self.channels:
                raise ValueError(f"channel {tentacle.id!r} already connected")
            tentacle.log_color = tentacle.brand_color or log_style_for_index(
                len(self.agents) + len(self.channels)
            )
            self.channels[tentacle.id] = tentacle
        elif isinstance(tentacle, AgentTentacle):
            tentacle.octomate = self
            if tentacle.id in self.agents:
                raise ValueError(f"agent {tentacle.id!r} already connected")
            tentacle.log_color = tentacle.brand_color or log_style_for_index(
                len(self.agents) + len(self.channels)
            )
            self.agents[tentacle.id] = tentacle
        else:
            logger.warning(
                "Skipping unknown tentacle %s (%s)",
                tentacle.id,
                type(tentacle).__name__,
            )
            return tentacle
        # Mount the tentacle's HTTP surface now that it is bound and registered — a
        # router builder like the Vercel one looks itself up in `self.channels`.
        self.routers.extend(tentacle.routers())
        return tentacle

    def log_tag(self, logger_name: str) -> tuple[str, Style | None]:
        """A short display tag for a logger plus its color: the owning tentacle's
        id and dispatched color, or the logger name with our package prefix
        trimmed (and no color) for shared/library loggers."""
        for tentacle in (*self.agents.values(), *self.channels.values()):
            if any(
                logger_name == prefix or logger_name.startswith(f"{prefix}.")
                for prefix in tentacle.log_names
            ):
                return tentacle.id, tentacle.log_color
        tag = short_log_name(logger_name)
        return tag, SHARED_LOG_STYLES.get(tag)

    async def kick(
        self,
        signal: AwakeSignal,
    ) -> None:
        """Trigger the agent graph from a user message turn or deferred response."""
        with octomate_logfire.span(
            "kick {signal_type}", signal_type=type(signal).__name__
        ) as span:
            if isinstance(signal, UserMessageSignal) and signal:
                address = signal.address
                span.set_attribute("channel_id", address.channel_tentacle_id)
                span.set_attribute("conversation_address", str(address))
            elif isinstance(signal, DeferredActionBatchResponse):
                span.set_attribute("batch_id", str(signal.batch_id))
                # Deliver the response to a live Claude run blocked on this batch
                # (approval/question), rather than resuming through the graph.
                for agent in self.agents.values():
                    if not agent.in_process:
                        continue
                    future = agent.pending.get(signal.batch_id)
                    if future is None:
                        continue
                    if not future.done():
                        future.set_result(signal)
                    span.set_attribute("resolved_live", agent.id)
                    return
            with sqlalchemy_materia():
                await reflex_graph.run(
                    inputs=Awake(signal=signal),
                    state=ReflexState(),
                    deps=ReflexDeps(
                        agents=self.agents,
                        channels=self.channels,
                        conversation_manager=self.conversations,
                        thread_manager=self.thread_manager,
                        action_manager=self.deferred_actions,
                    ),
                )

    def app(self, *, title: str = "Octomate") -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            with sqlalchemy_materia():
                # The identity registry reconciles before any tentacle starts, so
                # YAML users and their declared profiles exist from the first
                # ingested message; every other sender remains a visitor.
                await self.users.reconcile()
                # Each tentacle is an async context manager owning its own
                # long-lived resources (agents: warm MCP sessions; channels:
                # the inbound receive loop). Enter agents first so their tools
                # are warm before channels start ingesting; the stack tears
                # everything down in reverse on shutdown.
                async with AsyncExitStack() as stack:

                    async def start(tentacle: Tentacle) -> None:
                        # Isolate + time-bound each start so one slow or hung
                        # tentacle can't stall the others' startup. A failed start
                        # is logged and skipped, not fatal — the rest still serve.
                        try:
                            await asyncio.wait_for(
                                stack.enter_async_context(tentacle),
                                timeout=TENTACLE_START_TIMEOUT,
                            )
                        except Exception:
                            logger.exception(
                                "Tentacle %s failed to start; serving without it",
                                tentacle.id,
                            )

                    # Agents first (concurrently), then channels (concurrently), so
                    # tools are warm before channels ingest without any one tentacle
                    # blocking the group.
                    await asyncio.gather(
                        *(start(agent) for agent in self.agents.values())
                    )
                    await asyncio.gather(
                        *(start(channel) for channel in self.channels.values())
                    )
                    yield

        app = FastAPI(title=title, docs_url="/docs", redoc_url=None, lifespan=lifespan)
        app.state.octomate = self

        @app.middleware("http")
        async def activate_materia(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            with sqlalchemy_materia():
                return await call_next(request)

        for router in self.routers:
            app.include_router(router)

        return app
