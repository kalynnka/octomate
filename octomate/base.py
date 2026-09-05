from __future__ import annotations

import asyncio
import colorsys
import logging
import zlib
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import InitVar, dataclass, field
from functools import lru_cache
from itertools import count
from typing import TypeVar

from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import SecretStr
from rich.color import Color
from rich.style import Style

from octomate.config.base import OctomateConfig
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.managers.gateway import GatewayManager
from octomate.managers.oauth import OAuthManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import MirrorManager, WorkspaceManager
from octomate.mcp.base import KnownBearers
from octomate.mcp.gateway import served_session
from octomate.mcp.server import octomate_mcp
from octomate.oauth.routes import oauth_router
from octomate.reflex import (
    Awake,
    ReflexDeps,
    ReflexState,
    reflex_graph,
)
from octomate.schemas.awakes import (
    AwakeSignal,
    DeferredActionBatchResponse,
    GatewayHandoffSignal,
    UserMessageSignal,
)
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.oauth import DirectHttpOAuthCallbackTransport
from octomate.telemetry import octomate_logfire
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel import ChannelTentacle
from octomate.tentacles.mcp import McpTentacle

TentacleT = TypeVar("TentacleT", bound=Tentacle)
logger = logging.getLogger(__name__)

# Backstop for a tentacle whose startup hangs (a WebSocket connect that never
# completes, an unbounded probe). Past this, the host gives up on that tentacle
# and serves the rest rather than never finishing startup.
TENTACLE_START_TIMEOUT = 30.0

GOLDEN_RATIO_CONJUGATE = 0.618033988749895


def log_styles() -> Iterator[Style]:
    """Distinct, evenly-spread console colors, one per tentacle that claims none of
    its own — golden-ratio hue stepping keeps successive ones far apart on the
    color wheel without a fixed palette."""
    for index in count():
        hue = (index * GOLDEN_RATIO_CONJUGATE) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.6, 1.0)
        yield Style(color=Color.from_rgb(red * 255, green * 255, blue * 255), bold=True)


def short_log_name(name: str) -> str:
    """Chop a logger to its module so tags stay one word: our package prefix
    trims to the subsystem (`octomate.reflex.graph` -> `reflex`,
    `octomate.tentacles.feelers.output` -> `feelers`), and a library logger to
    its top-level module (`mcp.client.streamable_http` -> `mcp`; this also
    folds uvicorn's confusingly-named `uvicorn.error` into `uvicorn`)."""
    for prefix in ("octomate.tentacles.", "octomate."):
        if name.startswith(prefix):
            return name[len(prefix) :].split(".")[0]
    return name.split(".")[0]


# Shared subsystems front no tentacle, so they have neither a brand to claim nor a
# connection index to step the wheel with. The host's own voice and the plumbing every
# channel shares — the channel component and the feelers — take a neutral color here,
# so they read as the structure around the tentacles rather than as another hue
# competing with them.
SHARED_LOG_STYLES: dict[str, Style] = {
    "main": Style(color="bright_white", bold=True),
    "channel": Style(color="grey62", bold=True),
    "feelers": Style(color="grey62", bold=True),
}


@lru_cache(maxsize=256)
def muted_log_style(tag: str) -> Style:
    """A stable, muted hue per library tag — hashed rather than dispatched, so
    `httpx` wears the same color every run without claiming a connection index.
    The golden-ratio step spreads the hashes over the wheel (a plain modulo let
    `httpx` and `uvicorn.access` land one degree apart). Low saturation, no
    bold: libraries stay beneath the tentacles' saturated identity colors."""
    hue = (zlib.crc32(tag.encode()) * GOLDEN_RATIO_CONJUGATE) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.35, 0.85)
    return Style(color=Color.from_rgb(red * 255, green * 255, blue * 255))


@dataclass
class Octomate:
    """Application host for shared services, tentacles, and routers."""

    thread_manager: ThreadManager = field(init=False)
    conversations: ConversationManager = field(default_factory=ConversationManager)
    deferred_actions: DeferredActionManager = field(
        default_factory=DeferredActionManager
    )
    users: UserManager = field(default_factory=UserManager)
    workspaces: WorkspaceManager = field(default_factory=WorkspaceManager)
    gateway: GatewayManager = field(default_factory=GatewayManager)
    # The deployment config the host was built from. What a tentacle reads for
    # serving facts the app object itself does not model — above all the uvicorn
    # bind port, which only the config knows.
    config: OctomateConfig | None = None
    oauth_encryption_key: InitVar[SecretStr | None] = None
    oauth: OAuthManager = field(init=False)
    # Every connected tentacle by id, in connection order — the one registry. A
    # tentacle composes its roles (a channel that is also an MCP provider), so
    # the typed views below are readings of this dict, taken fresh each time: a
    # router builder reads `channels` while `connect` is still mounting.
    tentacles: dict[str, Tentacle] = field(default_factory=dict)
    # The next console color for a tentacle with no brand of its own.
    log_styles: Iterator[Style] = field(
        default_factory=log_styles, init=False, repr=False
    )
    routers: list[APIRouter] = field(default_factory=list)
    # Fire-and-forget graph turns (`kick_soon`), held strongly until they settle.
    background: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    # Every credential this deployment accepts — the registered users' own secrets,
    # nothing else. One registry shared by the MCP verifier and the hook guards;
    # with no user registered it rejects every bearer, and whether that should
    # refuse a boot is the hook routers' own mounting question.
    bearers: KnownBearers = field(init=False)

    def __post_init__(self, oauth_encryption_key: SecretStr | None) -> None:
        # Every ledger row references its sender's registry profile, so the
        # thread manager records through the host's one identity registry.
        self.thread_manager = ThreadManager(users=self.users)
        self.oauth = OAuthManager(
            users=self.users,
            encryption_key=oauth_encryption_key,
        )
        self.bearers = KnownBearers(
            self.config.users if self.config is not None else {}
        )

    @property
    def projects(self) -> ProjectManager:
        """The project registry. Kept by the workspace manager, which is the one
        thing that cannot work without it, and reached here because most of what
        asks — session attribution, the console, dispatch — wants the registry
        rather than anything to do with workspaces."""
        return self.workspaces.projects

    @property
    def mirrors(self) -> MirrorManager:
        """Every project's mirror, kept beside the registry for the same reason."""
        return self.workspaces.mirrors

    @property
    def agents(self) -> dict[str, AgentTentacle]:
        """The tentacles that run turns, by id."""
        return {
            id: tentacle
            for id, tentacle in self.tentacles.items()
            if isinstance(tentacle, AgentTentacle)
        }

    @property
    def channels(self) -> dict[str, ChannelTentacle]:
        """The tentacles that front a platform, by id."""
        return {
            id: tentacle
            for id, tentacle in self.tentacles.items()
            if isinstance(tentacle, ChannelTentacle)
        }

    @property
    def mcps(self) -> dict[str, McpTentacle]:
        """The tentacles that proxy a provider's MCP server, by id — the providers
        the served server's link tools know. The proxy is built per class, its
        tools resolving the instance — and the caller's own credential for it —
        from the session a call names, so two tentacles of one type share one."""
        return {
            id: tentacle
            for id, tentacle in self.tentacles.items()
            if isinstance(tentacle, McpTentacle)
        }

    def connect(self, tentacle: TentacleT) -> TentacleT:
        if tentacle.id in self.tentacles:
            raise ValueError(f"tentacle {tentacle.id!r} already connected")
        tentacle.octomate = self
        tentacle.log_color = tentacle.brand_color or next(self.log_styles)
        self.tentacles[tentacle.id] = tentacle
        # Mount the tentacle's HTTP surface now that it is bound and registered — a
        # router builder like the Vercel one looks itself up in `self.channels`.
        self.routers.extend(tentacle.routers())
        return tentacle

    def log_tag(self, logger_name: str) -> tuple[str, Style | None]:
        """A short display tag for a logger plus its color: the owning tentacle's
        id and dispatched color; a neutral style for the host's own subsystems;
        a stable muted hue for everything else, so every module is tellable at
        a glance."""
        for tentacle in self.tentacles.values():
            if any(
                logger_name == prefix or logger_name.startswith(f"{prefix}.")
                for prefix in tentacle.log_names
            ):
                return tentacle.id, tentacle.log_color
        tag = short_log_name(logger_name)
        return tag, SHARED_LOG_STYLES.get(tag) or muted_log_style(tag)

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
            elif isinstance(signal, GatewayHandoffSignal):
                span.set_attribute("agent_id", signal.agent_id)
                span.set_attribute("action", signal.decision.action)
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
                        workspaces=self.workspaces,
                        agents=self.agents,
                        channels=self.channels,
                        conversation_manager=self.conversations,
                        thread_manager=self.thread_manager,
                        action_manager=self.deferred_actions,
                        gateway=self.gateway,
                    ),
                )

    def kick_soon(self, signal: AwakeSignal) -> None:
        """`kick` as its own task, for a caller that must answer now — a served
        native spell whose handoff is a whole agent turn it cannot wait out."""
        task = asyncio.create_task(self.kick(signal))
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def app(self, *, title: str = "Octomate") -> FastAPI:
        # The one MCP server, which every runtime's install config knows as
        # `/octomate/mcp`, resolving the identity a call runs against from the
        # request itself. Always served, never open: the gateway's spells send to
        # real channels and hand conversations to other agents, so every call
        # authenticates against the registered users' own secrets — which locks

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            with sqlalchemy_materia():
                # The identity registry reconciles before any tentacle starts, so
                # YAML users and their declared profiles exist from the first
                # ingested message; every other sender remains a visitor.
                await self.users.reconcile()
                # Likewise the project registry: reconciling here is what builds
                # the resolution index, so a declared project resolves before
                # anything is running that could ask.
                await self.projects.reconcile()
                # How a workspace is forked is the filesystem's answer, not a
                # setting: probed here so the log says which mechanism this host
                # got, once, before anything asks for a workspace.
                await self.workspaces.detect()
                # Each tentacle is an async context manager owning its own
                # long-lived resources (agents: warm MCP sessions; channels:
                # the inbound receive loop). Channels live on the inner stack so
                # shutdown closes them first — nothing ingests into agents whose
                # sessions are already torn down; every other tentacle is on
                # the outer one.
                async with (
                    # Starlette runs no lifespan for a mounted app, and the MCP
                    # transport's task group lives in that lifespan; the endpoint
                    # answers only inside it. Outermost, so the server is up
                    # before any tentacle starts and down after the last stops.
                    mcp_app.lifespan(mcp_app),
                    AsyncExitStack() as outer_stack,
                    AsyncExitStack() as channel_stack,
                ):

                    async def start(stack: AsyncExitStack, tentacle: Tentacle) -> None:
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

                    async def start_all() -> None:
                        # Everything at once: channels must not queue behind agent
                        # warmup, which is an optimization, not a precondition — a
                        # message landing before its agent finished warming enters
                        # the cold toolsets inside its own run (reference-counted)
                        # and pays the listing latency once.
                        await asyncio.gather(
                            *(
                                start(
                                    channel_stack
                                    if isinstance(tentacle, ChannelTentacle)
                                    else outer_stack,
                                    tentacle,
                                )
                                for tentacle in self.tentacles.values()
                            )
                        )

                    # Serve immediately: MCP warms and channel sockets proceed in
                    # the background so a console connects the moment uvicorn
                    # binds. `start` already isolates and time-bounds each entry,
                    # so this task settles on its own and never raises.
                    starting = asyncio.create_task(start_all())
                    # Mirrors in the background too: a first clone takes as long
                    # as the repository is big, and serving must not wait on it.
                    # `reconcile` isolates per-project failures itself.
                    mirroring = asyncio.create_task(
                        self.mirrors.reconcile(self.projects.list())
                    )
                    # Reclaiming disk is maintenance: it runs for as long as
                    # the host does, and stops when the host stops.
                    sweeping = asyncio.create_task(self.workspaces.sweep())
                    try:
                        yield
                    finally:
                        # Join before the enclosing stack exits — on every path.
                        # An error thrown into the yield would otherwise unwind
                        # the stack while `start_all` is still pushing entries
                        # onto it. `start` bounds each entry, so this wait is
                        # bounded too; on a normal shutdown it is a no-op.
                        await starting
                        # Cancelled rather than awaited: a mirror sync is not
                        # bounded the way `start` is, and creation cleans up
                        # after a cancellation, so shutdown stays prompt.
                        mirroring.cancel()
                        sweeping.cancel()
                        with suppress(asyncio.CancelledError):
                            await mirroring
                            await sweeping

        app = FastAPI(title=title, docs_url="/docs", redoc_url=None, lifespan=lifespan)
        app.state.octomate = self

        @app.middleware("http")
        async def activate_materia(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            with sqlalchemy_materia():
                return await call_next(request)

        # The OAuth router is the project's own, not a tentacle's, and it is mounted
        # only when a registered connector actually points a browser at it — the two
        # routes are the deployment's public surface, and a deployment with no
        # authorization-code integration should not be serving them at all.
        if any(
            isinstance(connector.callback_transport, DirectHttpOAuthCallbackTransport)
            for connector in self.oauth.connectors.values()
        ):
            app.include_router(oauth_router)

        for router in self.routers:
            app.include_router(router)

        # A mounted app rather than a router: the MCP transport speaks all three
        # methods on one path, reads and writes the stream itself, and carries
        # its own bearer check — the deployment's known bearers, the same
        # credentials and principals as the hook routers, which locks the
        # endpoint outright until a user is registered.
        mcp = octomate_mcp(
            served_session(self),
            self.thread_manager,
            kick=self.kick_soon,
            bearers=self.bearers,
            tentacles=list(self.mcps.values()),
        )
        # Stateless: identity is per call, from the request, so there is nothing
        # for the transport to keep between calls. Mounted under the server's name
        # below, this path is the tail of `OCTOMATE_MCP_PATH`.
        mcp_app = mcp.http_app(path="/mcp", stateless_http=True)
        app.mount(f"/{mcp.name}", mcp_app, name=mcp.name)

        return app
