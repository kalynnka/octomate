from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from octomate.schemas.thread import DEEPSEEK_NATIVE_ID, ThreadKey
from octomate.schemas.user import UserProfile
from octomate.telemetry import deepseek_logfire
from octomate.tentacles.agents.deepseek.hooks import DeepseekHookInput
from octomate.tentacles.agents.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate

    # Runtime dependency runs the other way (the tailer imports this module's
    # NATIVE_USER); the injected instance only needs its type here.
    from octomate.tentacles.agents.deepseek.tailer import DeepseekEventTailer

logger = logging.getLogger(__name__)

# A native session carries no platform identity for whoever is typing.
NATIVE_USER = UserProfile(channel_user_id="native", name="native")


class DeepseekHookIngest:
    """The hook pipe's half of native-session ingest — and deliberately the
    thin half: dsh's Claude-Code hook dialect carries no per-turn key and no
    answer, so every durable row is written by the `DeepseekEventTailer` from
    the streamed session log (`octomate deepseek tail`), and a hook is the
    session skeleton plus the stream's turn boundary.

    `Stop` fires on dsh's *blocking* turn-stopping seam, ahead of the very
    `turn/end` the settle waits for — so its work is spawned detached and the
    hook returns at once: an ingest awaited there would deadlock the seam
    against its own flush.
    """

    def __init__(
        self,
        octomate: Octomate,
        tailer: DeepseekEventTailer,
        locks: SessionLocks | None = None,
    ) -> None:
        self.octomate = octomate
        self.tailer = tailer
        self.locks = locks if locks is not None else SessionLocks()
        # Sessions the tentacle is driving right now, counted by how many runs
        # hold each — their hooks fire like any native session's (the bridge's
        # config is process-wide in a dsh octomate may only have attached to),
        # but the tentacle records those runs itself, and the stream route
        # refuses their tails.
        self.driven: Counter[str] = Counter()
        self.tasks: set[asyncio.Task[None]] = set()

    @contextmanager
    def driving(self, session_id: str) -> Generator[None]:
        """Claim a session as one Octomate drives itself, for the length of the
        turn. Taken before `session.prompt` goes out, so the prompt's own hooks
        arrive claimed; dsh's `Stop` fires inside turn-stopping, before the
        `turn/end` frame that ends the claim's scope, so it arrives claimed too.
        """
        self.driven[session_id] += 1
        try:
            yield
        finally:
            self.driven[session_id] -= 1
            if self.driven[session_id] <= 0:
                del self.driven[session_id]

    async def handle(self, event: DeepseekHookInput) -> None:
        if not event.session_id:
            return
        if event.session_id in self.driven:
            logger.debug("session %s: ignored driven dsh hook", event.session_id)
            return
        match event.hook_event_name:
            case "UserPromptSubmit":
                await self.on_user_prompt_submit(event)
            case "Stop":
                await self.on_stop(event)

    @deepseek_logfire.instrument(
        "deepseek.hook UserPromptSubmit [{event.session_id}]", extract_args=["event"]
    )
    async def on_user_prompt_submit(self, event: DeepseekHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
        logger.info("session %s: prompt observed", event.session_id)

    @deepseek_logfire.instrument(
        "deepseek.hook Stop [{event.session_id}]", extract_args=["event"]
    )
    async def on_stop(self, event: DeepseekHookInput) -> None:
        # The stream's drain boundary; the settle runs detached because it
        # waits for a `turn/end` dsh only emits after this hook returns.
        task = asyncio.create_task(self.tailer.stop_turn(event.session_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        logger.info(
            "session %s: turn stopped; settling from the stream", event.session_id
        )

    async def start_session(self, event: DeepseekHookInput) -> None:
        """Ensure the session's skeleton (thread + conversation), filed under
        the project already holding the directory it runs in — resolve-only, as
        on the Claude side: a dsh session is not evidence of a project. The
        stream's attach does the same, but a hook's cwd lands first and a
        thread's project is frozen at creation."""
        holder = self.octomate.projects.resolve(Path(event.cwd)) if event.cwd else None
        project = self.octomate.projects.get(holder) if holder is not None else None
        thread = await self.octomate.thread_manager.ensure(
            ThreadKey(DEEPSEEK_NATIVE_ID, "thread", event.session_id),
            project=project,
        )
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=DEEPSEEK_NATIVE_ID
        )

    def shutdown(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        self.tasks.clear()
