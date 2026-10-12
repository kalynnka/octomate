"""The Trunkline web channel: the console drives Octomate through
`octomate.kick` and reads the run stream back natively.

Unlike the Vercel dev-UI channel it replaces, nothing is transcoded to a
third-party UI protocol: the run's own event union (the harness `WireEvent`)
is what the console renders, read off the nerve. The channel draws nothing
through feelers: its siphon holds a subscription for each live socket, on the
thread the socket watches, whichever channel it is on, and sends what the
nerve carries there down it, where the kick's `reflex_asleep` arrives last. A
directive, a batch answer or a thread operation is answered as soon as its
kick is on its way. Keyed as a
`flat_thread` channel, every directive carries its thread id and streams
straight through without triage — a new id starts a new thread, an existing
id continues it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from rich.style import Style
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import (
    WireEvent,
    wire_event_adapter,
)
from octomate.config.channels import AgentModelConfig, TrunklineChannelConfig
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.project import Project
from octomate.schemas.segments import (
    ImageSegment,
    TextSegment,
)
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.channel import (
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    IMMessageID,
    Ink,
    ThreadStrategy,
)
from octomate.tentacles.siphon import Siphon
from octomate.types.permissions import AgentPermissionMode

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

# Separator joining agent and model into the route id the console picker offers.
ROUTE_SEP = ":"


class TrunklineDirective(BaseModel):
    """One console turn: the directive text bound for a thread."""

    thread_id: str = Field(min_length=1)
    user: User
    text: str
    message_id: str | None = None
    model: str | None = None
    project: str | None = None
    permission_mode: AgentPermissionMode | None = None
    effort: str | None = Field(
        default=None, description="The model's effort selection, stored before the run."
    )


class RouteLockedError(Exception):
    """A directive tried to re-route a thread that already has an owner."""


class TrunklineSeamNotWired(NotImplementedError):
    """Raised by the platform send/encode seam, unused by the inline-SSE path."""

    def __init__(self) -> None:
        super().__init__(
            "The Trunkline channel streams inline over SSE; the platform "
            "send/encode seam is unused by the console delivery path."
        )


class TrunklineInk(Ink[WireEvent]):
    """Console identity and thread locations; output streams inline."""

    async def inspect(self) -> UserProfile:
        return UserProfile(channel_user_id="trunkline", name="Trunkline")

    async def suggest_addresses(
        self, address: ChannelAddress, source_address: ChannelAddress | None = None
    ) -> list[ChannelAddress]:
        return [
            ChannelAddress(
                address.channel_tentacle_id, "thread", address.user_id, address.user_id
            )
        ]

    async def prepare_address(
        self,
        address: ChannelAddress,
        source_address: ChannelAddress | None = None,
        *,
        private: bool = False,
    ) -> ChannelAddress:
        if (
            address.chat_type != "thread"
            or address.chat_id != address.user_id
            or address.shared
            or address.channel_thread_id
        ):
            raise ValueError("Trunkline requires a new thread for its requesting user.")
        return address

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(channel_user_id=user_id, name="Console")

    async def upload_media(self, data: bytes) -> str | None:
        raise TrunklineSeamNotWired

    async def download_image(
        self, seg: ImageSegment, message_id: str
    ) -> DownloadedImage | None:
        raise TrunklineSeamNotWired

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[WireEvent],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        raise TrunklineSeamNotWired


class TrunklineChromo(Chromo[TrunklineDirective, WireEvent]):
    """Inbound translation only; outbound encoding is unused."""

    async def sip(self, raw: TrunklineDirective) -> MessageEvent | None:
        if not raw.text.strip():
            return None
        return MessageEvent(
            message_id=raw.message_id or uuid7().hex,
            channel_thread_id=raw.thread_id,
            user_id=str(raw.user.id),
            chat_id=str(raw.user.id),
            chat_type="thread",
            segments=[TextSegment(data={"text": raw.text})],
            raw=raw.text,
        )

    def outbound_markdown(self, text: str) -> list[WireEvent]:
        raise TrunklineSeamNotWired


# How far behind a console's socket may fall before it is closed rather than
# held for: the console reads the thread again when it reopens.
CONSOLE_BACKLOG = 4096


class TrunklineSiphon(Siphon):
    """The console's siphon. It subscribes to nothing of its own: a console
    picks the threads it watches, on any channel, and each live socket holds a
    subscription to its thread for as long as it is open."""

    def start(self) -> None:
        """Nothing to subscribe to until a socket opens."""

    async def watch(self, websocket: WebSocket, key: ThreadKey) -> None:
        """Hold the console's socket on a thread until the console closes it,
        sending each event the nerve carries to the thread down it meanwhile.
        The subscription is open before the handshake completes, so a request
        the console sends once the socket is open misses nothing. A console
        `CONSOLE_BACKLOG` events behind is closed with 1013 to read the thread
        again; one that is gone ends the same way, once its close is read."""
        nerve = self.channel.octomate.nerve
        with nerve.subscribe(key, backlog=CONSOLE_BACKLOG) as impulses:
            await websocket.accept()

            async def send() -> None:
                async for impulse in impulses:
                    await websocket.send_text(
                        wire_event_adapter.dump_json(
                            impulse.event, warnings=False
                        ).decode()
                    )
                await websocket.close(code=1013, reason="fell behind")

            sending = asyncio.create_task(send())
            try:
                # The console sends nothing; reading is how its close is noticed.
                while (await websocket.receive())["type"] != "websocket.disconnect":
                    pass
            finally:
                sending.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await sending


class ConsoleFiles(StaticFiles):
    """The console's build, served at `/`.

    A browser asking for a path that names no file is asking for one of the
    console's own pages — the thread or control page its address names — so it is
    answered with `index.html`, and a reload or a shared link opens where it
    points. Any other miss stays a 404, so an unknown API path is never answered
    with a page.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as missing:
            accept = Headers(scope=scope).get("accept", "")
            if missing.status_code != 404 or "text/html" not in accept:
                raise
            return await super().get_response("index.html", scope)


class TrunklineTentacle(ChannelTentacle[TrunklineDirective, WireEvent]):
    """Channel that serves the Trunkline web console over `octomate.kick`."""

    brand_color: ClassVar[Style | None] = Style(color="#D4621A", bold=True)

    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"

    config: TrunklineChannelConfig
    siphon: TrunklineSiphon

    def __init__(
        self, id: str, octomate: Octomate, *, config: TrunklineChannelConfig
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=TrunklineInk(),
            chromo=TrunklineChromo(),
            config=config,
        )
        self.siphon = TrunklineSiphon(self)

    def routers(self) -> tuple[APIRouter, APIRouter]:
        """The console HTTP surface and its live socket, mounted by
        `Octomate.connect`."""
        # Local import: routes.py imports this module, so a module-level import cycles.
        from octomate.tentacles.trunkline.routes import (
            build_trunkline_live_router,
            build_trunkline_router,
        )

        return (
            build_trunkline_router(self.octomate, channel_id=self.id),
            build_trunkline_live_router(self),
        )

    @property
    def agent_ids(self) -> list[str]:
        """All registered agents, with the configured entry order first."""
        return list(dict.fromkeys([*self.config.agents, *self.octomate.agents]))

    def routable_agents(self) -> list[AgentModelConfig]:
        """Every agent's models, with only the configured entry agent's default."""
        routes: list[AgentModelConfig] = []
        agents = self.octomate.agents
        for agent_id in self.agent_ids:
            agent = agents.get(agent_id)
            if agent is None or not agent.models:
                continue
            default_model: str | None = None
            if agent_id == self.config.agents[0]:
                default_model = agent.resolve_model()
                routes.append(AgentModelConfig(agent=agent_id, model=default_model))
            for model in agent.models:
                if model != default_model:
                    routes.append(AgentModelConfig(agent=agent_id, model=model))
        return routes

    def chosen_project(self, name: str | None) -> Project | None:
        """The registered project a directive named, or None when it named none.

        The console is where a project is chosen, since a console thread has no
        directory of its own to be filed from — and no project is a real answer: the
        thread is then a chat, and its agent works nowhere in particular. An
        unregistered name is refused rather than dropped, because a typo would
        otherwise read as that answer.
        """
        if name is None:
            return None
        project = self.octomate.projects.get(name)
        if project is None or not project.enabled:
            raise ValueError(f"no enabled project is registered as {name!r}")
        return project

    async def claim_route(self, thread: Thread, selected_model: str | None) -> None:
        """The route is chosen at thread creation only: the first directive's
        pick records the entry handoff. Once a thread has an owner the route is
        fixed — a mid-thread model switch busts the provider's KV cache — so a
        differing pick is refused until an explicit manual handoff exists."""
        if thread.active_agent_tentacle_id is not None:
            if selected_model is None:
                return
            active = f"{thread.active_agent_tentacle_id}{ROUTE_SEP}{thread.active_model or ''}"
            if selected_model == active:
                return
            raise RouteLockedError(
                f"thread is routed to {active!r}; the route is fixed after the "
                "first directive — switching needs a manual handoff"
            )
        if selected_model is None:
            return
        chosen = next(
            (
                agent_config
                for agent_config in self.routable_agents()
                if f"{agent_config.agent}{ROUTE_SEP}{agent_config.model or ''}"
                == selected_model
            ),
            None,
        )
        if chosen is None:
            return
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=chosen.agent
        )
        await self.octomate.threads.record_handoff(
            thread,
            source_agent_tentacle_id=None,
            to_agent_tentacle_id=chosen.agent,
            to_model=chosen.model,
            reason="console route selection",
            target_conversation_id=conversation.id,
        )

    def thread_user_id(self, profile: UserProfile) -> str | None:
        return str(profile.user_id) if profile.user_id is not None else None

    def is_shared(self, address: ChannelAddress) -> bool:
        """Never: every console thread is its one user's."""
        return False

    @property
    def landing_unavailable(self) -> None:
        """Never: a landing here is a new thread of its own, under no parent."""
        return None

    async def start_thread(self, address: ChannelAddress, hint: str) -> ChannelAddress:
        if (
            address.channel_tentacle_id != self.id
            or address.chat_type != "thread"
            or address.channel_thread_id
            or address.chat_id != address.user_id
        ):
            raise ValueError("Trunkline requires a new thread for its requesting user.")
        return ChannelAddress(
            channel_tentacle_id=self.id,
            chat_type="thread",
            chat_id=address.chat_id,
            user_id=address.user_id,
            channel_thread_id=uuid7().hex,
        )

    async def claim_posture(self, thread: Thread, mode: AgentPermissionMode) -> None:
        """Store the posture a directive picked on the conversation about to run it.

        This is the console picking one before there is anything to pick it on: a
        thread being composed has no row yet, and by the time it does the run that
        would have honored the pick is already going. An owned thread's posture is
        switched directly on its conversation instead.

        Which agent it belongs to is the thread's own, claimed just above. A posture
        with no agent to read it is refused rather than stored against a guess — the
        vocabularies do not overlap, so guessing would be picking a provider.
        """
        agent_tentacle_id = thread.active_agent_tentacle_id
        if agent_tentacle_id is None:
            raise ValueError(
                "a permission mode is one agent's vocabulary and this thread is "
                "routed to none; pick a route on the directive that creates it"
            )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=agent_tentacle_id
        )
        await self.octomate.agents[agent_tentacle_id].set_permission_mode(
            conversation, mode
        )

    async def handle_directive(self, directive: TrunklineDirective) -> None:
        event = await self.chromo.sip(directive)
        if event is None:
            raise ValueError("directive carried no text")
        event.tentacle_id = self.id
        event.self_id = self.self_profile.channel_user_id
        event.sender = UserProfile(
            channel_tentacle_id=self.id,
            channel_user_id=event.user_id,
            user_id=directive.user.id,
            name=directive.user.name,
            nickname=directive.user.nickname,
        )
        thread = await self.octomate.threads.ensure(
            ChannelAddress(
                channel_tentacle_id=self.id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                user_id=event.user_id,
                channel_thread_id=event.channel_thread_id,
                shared=event.shared,
            ),
            project=self.chosen_project(directive.project),
        )
        await self.claim_route(thread, directive.model)
        if directive.permission_mode is not None:
            await self.claim_posture(thread, directive.permission_mode)
        if directive.effort is not None:
            agent_id = thread.active_agent_tentacle_id
            if agent_id is None:
                raise ValueError("Select a route before setting its effort")
            conversation = await self.octomate.conversations.ensure(
                thread.id, agent_tentacle_id=agent_id, with_history=False
            )
            effort = await self.octomate.agents[agent_id].resolve_effort(
                conversation, model=thread.active_model, effort=directive.effort
            )
            await self.octomate.conversations.set_effort(conversation, effort)
        thread_message = await self.octomate.threads.record_inbound(event)
        self.octomate.kick_soon(
            UserMessageSignal([event], trigger_thread_message_id=thread_message.id)
        )
