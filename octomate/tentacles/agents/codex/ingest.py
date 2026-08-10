from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    CODEX_NATIVE_ID,
    Thread,
    ThreadKey,
    ThreadMessageDirection,
)
from octomate.schemas.user import UserProfile
from octomate.telemetry import codex_logfire
from octomate.tentacles.agents.codex.hooks import CodexHookInput
from octomate.tentacles.agents.codex.transcript import CODEX_SESSIONS_DIRS
from octomate.tentacles.agents.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate
    from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer

NATIVE_USER = UserProfile(channel_user_id="native", name="native")
logger = logging.getLogger(__name__)


class CodexHookIngest:
    def __init__(
        self,
        octomate: Octomate,
        tailer: CodexTranscriptTailer,
        locks: SessionLocks | None = None,
        extra_transcript_roots: tuple[Path, ...] = (),
    ) -> None:
        self.octomate = octomate
        self.tailer = tailer
        self.locks = locks if locks is not None else SessionLocks()
        # The trees a hook may name a rollout in: Codex's own, plus any an operator adds.
        # A union rather than a replacement — the documented location keeps working
        # whatever else is configured, so naming an extra root can never be the reason a
        # session silently stops being ingested.
        #
        # Resolved once: the test is `is_relative_to`, which is lexical, so both sides
        # must be resolved for `..` to mean anything.
        self.transcript_roots = tuple(
            root.resolve() for root in (*CODEX_SESSIONS_DIRS, *extra_transcript_roots)
        )
        self.driven: Counter[str] = Counter()

    @contextmanager
    def driving(self, session_id: str) -> Generator[None]:
        self.driven[session_id] += 1
        try:
            yield
        finally:
            self.driven[session_id] -= 1
            if self.driven[session_id] <= 0:
                del self.driven[session_id]

    @codex_logfire.instrument(
        "codex.hook {event.hook_event_name} [{event.session_id}]",
        extract_args=["event"],
    )
    async def handle(self, event: CodexHookInput) -> None:
        if event.octomate_driven or event.session_id in self.driven:
            logger.debug("session %s: ignored driven Codex hook", event.session_id)
            return
        if event.hook_event_name == "SubagentStart":
            await self.on_subagent_start(event)
            return
        if event.hook_event_name == "SubagentStop":
            await self.on_subagent_stop(event)
            return
        # Child rollout ingestion owns other agent-scoped events.
        if event.agent_id is not None:
            return
        if event.hook_event_name == "Stop":
            await self.on_stop(event)
            return
        if event.hook_event_name in {"SessionStart", "UserPromptSubmit"}:
            async with self.locks.hold(event.session_id):
                await self.start_session(event)
                if event.hook_event_name == "UserPromptSubmit" and event.prompt:
                    await self.record_prompt(event, event.prompt)
                    await self.sketch_run(event)
                    logger.info(
                        "session %s: turn %s asked",
                        event.session_id,
                        event.turn_id,
                    )

    async def on_stop(self, event: CodexHookInput) -> None:
        async with self.locks.hold(event.session_id):
            if event.last_assistant_message:
                await self.record_answer(event, event.last_assistant_message)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s answered",
                    event.session_id,
                    event.turn_id,
                )
        # Stop is a turn boundary in Codex, not a session boundary. Pump after the hook
        # releases the shared lock; the rollout's task_complete line may now be durable.
        await self.tailer.pump_session(event.session_id)

    async def on_subagent_start(self, event: CodexHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
        await self.tailer.poke_subagents(event.session_id)
        logger.info(
            "session %s: Codex subagent %s started",
            event.session_id,
            event.agent_id,
        )

    async def on_subagent_stop(self, event: CodexHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
        if event.agent_transcript_path is None:
            return
        path = self.accepted_transcript_path(
            event.session_id, event.agent_transcript_path
        )
        if path is None:
            return
        await self.tailer.finish_subagent(
            event.session_id,
            path,
            agent_id=event.agent_id,
            final_answer=event.last_assistant_message,
        )
        logger.info(
            "session %s: Codex subagent %s stopped",
            event.session_id,
            event.agent_id,
        )

    async def start_session(self, event: CodexHookInput) -> None:
        if event.transcript_path is None:
            return
        path = self.accepted_transcript_path(event.session_id, event.transcript_path)
        if path is None:
            return
        if not path.parent.is_dir():
            return
        thread = await self.session_thread(event)
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )
        self.tailer.start(event.session_id, path)

    def accepted_transcript_path(self, session_id: str, claimed: Path) -> Path | None:
        # Resolved before the root test: `is_relative_to` is lexical, so an unresolved
        # `.../sessions/../../elsewhere` would pass it. The path is the caller's claim,
        # and following it means reading whatever it names into this session's history.
        path = claimed.resolve()
        if not any(path.is_relative_to(root) for root in self.transcript_roots):
            logger.warning(
                "session %s: refusing transcript outside %s: %s. If Codex writes "
                "elsewhere here, set agents.codex.transcript_root.",
                session_id,
                ", ".join(str(root) for root in self.transcript_roots),
                path,
            )
            return None
        return path

    async def session_thread(self, event: CodexHookInput) -> Thread:
        """This session's thread, attributed to the project it is running in.

        As on the Claude side: the session's own directory says which project this
        is, a directory no project holds yet becomes one, the project lands only when
        the thread is created, and a rollout root is never a project root. Only a cwd
        the hook carried — `Path("")` is the process's own directory, which would
        attribute every session to whatever project Octomate itself was started in.
        """
        project = None
        if event.cwd:
            project = await self.octomate.projects.ensure(
                Path(event.cwd), origin="codex"
            )
        return await self.octomate.thread_manager.ensure(
            ThreadKey(CODEX_NATIVE_ID, "thread", event.session_id),
            project=project,
        )

    async def record_prompt(self, event: CodexHookInput, prompt: str) -> None:
        thread = await self.session_thread(event)
        if event.turn_id and self.already_recorded(thread, event.turn_id, "inbound"):
            return
        await self.octomate.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=CODEX_NATIVE_ID,
                message_id=event.turn_id or "",
                chat_id=event.session_id,
                chat_type="thread",
                user_id=NATIVE_USER.channel_user_id,
                sender=NATIVE_USER,
                segments=[TextSegment(data={"text": prompt})],
            )
        )

    async def record_answer(self, event: CodexHookInput, answer: str) -> None:
        thread = await self.session_thread(event)
        if event.turn_id and self.already_recorded(thread, event.turn_id, "outbound"):
            return
        await self.octomate.thread_manager.record_outbound(
            thread,
            agent_tentacle_id=CODEX_NATIVE_ID,
            segments=[MarkdownSegment(data={"text": answer})],
            sender=NATIVE_USER,
            platform_message_id=event.turn_id or "",
        )

    async def sketch_run(self, event: CodexHookInput) -> None:
        if not event.turn_id:
            return
        thread = await self.session_thread(event)
        prompt = next(
            (
                message
                for message in thread.messages
                if message.platform_message_id == event.turn_id
                and message.direction == "inbound"
            ),
            None,
        )
        if prompt is None or prompt.message_text is None:
            return
        messages: list[ModelMessage] = [
            ModelRequest(
                parts=[UserPromptPart(prompt.message_text)],
                timestamp=prompt.happened_at,
            )
        ]
        if event.last_assistant_message:
            messages.append(
                ModelResponse(parts=[TextPart(event.last_assistant_message)])
            )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CODEX_NATIVE_ID
        )
        await self.octomate.conversations.record_external_run(
            conversation,
            run_id=event.turn_id,
            messages=messages,
            name=CODEX_NATIVE_ID,
            cwd=event.cwd,
            external_session_id=event.session_id,
        )

    @staticmethod
    def already_recorded(
        thread: Thread, turn_id: str, direction: ThreadMessageDirection
    ) -> bool:
        return any(
            message.platform_message_id == turn_id and message.direction == direction
            for message in thread.messages
        )
