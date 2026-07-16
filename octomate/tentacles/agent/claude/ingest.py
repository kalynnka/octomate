from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import logfire
from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from octomate.schemas.conversation import UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    Thread,
    ThreadKey,
    ThreadMessage,
    ThreadMessageDirection,
)
from octomate.tentacles.agent.claude.hooks import ClaudeHookInput
from octomate.tentacles.agent.claude.locks import SessionLocks

if TYPE_CHECKING:
    from octomate import Octomate

    # Runtime dependency runs the other way (the tailer imports this module's
    # CLAUDE_NATIVE_ID); the injected instance only needs its type here.
    from octomate.tentacles.agent.claude.tailer import ClaudeTranscriptTailer

logger = logging.getLogger(__name__)

# Synthetic channel/agent id marking a thread as ingested from a native Claude client
# rather than driven by a live tentacle. No channel is registered under it: an ingested
# thread is recorded, never dispatched to.
CLAUDE_NATIVE_ID = "claude-native"

# A native session carries no platform identity for whoever is typing.
NATIVE_USER = UserProfile(user_id="native", name="native")


class ClaudeHookIngest:
    """Live human-ledger ingest for a native Claude Code session, and the lifecycle that
    starts and finalizes its transcript tailer.

    Each turn's prompt (`UserPromptSubmit`) and answer (`Stop.last_assistant_message`)
    are written as inbound/outbound `ThreadMessage`s — a complete, lossless chat log,
    visible the moment the session runs — and sketched as the turn's provisional
    `ExternalAgentRun`, so a turn in flight is a whole conversation → run → messages
    chain and not a chat log with no model history to hang from. The events carry no
    thinking, tools or usage, so the full timeline comes from the
    `ClaudeTranscriptTailer`, which this starts on the first prompt (`SessionStart` is
    not delivered to http hooks) and finalizes at `SessionEnd`; it replaces each sketch
    as its turn closes. No transcript is read here.

    Ledger rows and the run alike are keyed by `prompt_id` — `platform_message_id` on
    the rows, the run's own id — the stable per-turn key both tiers write under, so the
    tailer's rebuilt run and the hooks' sketch are always the same run.
    """

    def __init__(
        self,
        octomate: Octomate,
        tailer: ClaudeTranscriptTailer,
        locks: SessionLocks | None = None,
    ) -> None:
        self.octomate = octomate
        self.tailer = tailer
        # Serialize a session's events so the existence check and the write can't race
        # (Claude fires the next event without waiting for our commit). Shared with the
        # tailer so its run/ledger commits serialize against these writes too; the
        # registry reclaims a session's lock on its own once no one holds it.
        self.locks = locks if locks is not None else SessionLocks()
        # Sessions the tentacle is driving right now, counted by how many runs hold
        # each (`driving`) — a follow-up run supersedes a live one and the two overlap
        # while the first unwinds, so the claim outlives whichever ends first.
        self.driven: Counter[str] = Counter()

    @contextmanager
    def driving(self, session_id: str) -> Generator[None]:
        """Claim a session as one Octomate drives itself, for the length of the run.

        Its hooks fire like any other session's — an operator's settings are theirs to
        keep, and this pipe does not reach around them — but the tentacle records the
        run as it drives it, so ingesting the hooks would write that conversation a
        second time.

        The claim brackets the run rather than tracking its events: taken before the CLI
        exists, so no hook can arrive unclaimed, and dropped only once the client's
        teardown has waited out the process, by which point every hook the session can
        fire — `SessionEnd` last, on its way out — already has.
        """
        self.driven[session_id] += 1
        try:
            yield
        finally:
            self.driven[session_id] -= 1
            if self.driven[session_id] <= 0:
                del self.driven[session_id]

    async def handle(self, event: ClaudeHookInput) -> None:
        if event.session_id in self.driven:
            return
        match event.hook_event_name:
            case "SessionStart":
                await self.on_session_start(event)
            case "UserPromptSubmit":
                await self.on_user_prompt_submit(event)
            case "Stop":
                await self.on_stop(event)
            case "SessionEnd":
                await self.on_session_end(event)

    @logfire.instrument(
        "claude.hook SessionStart [{event.session_id}]", extract_args=["event"]
    )
    async def on_session_start(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            await self.start_session(event)

    @logfire.instrument(
        "claude.hook UserPromptSubmit [{event.session_id}]", extract_args=["event"]
    )
    async def on_user_prompt_submit(self, event: ClaudeHookInput) -> None:
        # SessionStart is not delivered to http hooks, so the first prompt starts the
        # tailer if it is not already following (self-heal).
        async with self.locks.hold(event.session_id):
            await self.start_session(event)
            if event.prompt:
                await self.record_prompt(event, event.prompt)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s asked", event.session_id, event.prompt_id
                )

    @logfire.instrument("claude.hook Stop [{event.session_id}]", extract_args=["event"])
    async def on_stop(self, event: ClaudeHookInput) -> None:
        async with self.locks.hold(event.session_id):
            if event.last_assistant_message:
                await self.record_answer(event, event.last_assistant_message)
                await self.sketch_run(event)
                logger.info(
                    "session %s: turn %s answered", event.session_id, event.prompt_id
                )

    @logfire.instrument(
        "claude.hook SessionEnd [{event.session_id}]", extract_args=["event"]
    )
    async def on_session_end(self, event: ClaudeHookInput) -> None:
        logger.info("session %s: ended", event.session_id)
        # Finalize outside any lock: it awaits the follow loop's own last commit, which
        # takes the session lock — holding it here would deadlock.
        await self.tailer.finalize(event.session_id, event.transcript_path)

    async def start_session(self, event: ClaudeHookInput) -> None:
        """Ensure the session's skeleton (thread + conversation) and start its transcript
        tailer, once per session. Ensuring the skeleton here — under the session lock,
        before the follow loop runs — is what keeps the tailer's own `ensure` a cache hit
        rather than a write that could race the hooks' first-sighting create.

        Requires the transcript directory to exist (the tailer's watch needs it); a hook
        carrying no usable path leaves the tailer unstarted, and the ledger still writes.
        """
        path = event.transcript_path
        if self.tailer.is_following(event.session_id) or path is None:
            return
        if not path.parent.is_dir():
            return
        thread = await self.session_thread(event.session_id)
        await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        self.tailer.start(event.session_id, path)
        logger.info("session %s: tailing %s", event.session_id, path)

    async def session_thread(self, session_id: str) -> Thread:
        return await self.octomate.thread_manager.ensure(
            ThreadKey(
                channel_tentacle_id=CLAUDE_NATIVE_ID,
                chat_type="private",
                chat_id=session_id,
                thread_id="",
            )
        )

    async def record_prompt(self, event: ClaudeHookInput, prompt: str) -> None:
        thread = await self.session_thread(event.session_id)
        if event.prompt_id and self.already_recorded(
            thread, event.prompt_id, "inbound"
        ):
            return
        await self.octomate.thread_manager.record_inbound(
            MessageEvent(
                tentacle_id=CLAUDE_NATIVE_ID,
                message_id=event.prompt_id or "",
                chat_id=event.session_id,
                chat_type="private",
                user_id=NATIVE_USER.user_id,
                sender=NATIVE_USER,
                segments=[TextSegment(data={"text": prompt})],
            )
        )

    async def sketch_run(self, event: ClaudeHookInput) -> None:
        """Write the turn's provisional run — the model timeline as the hooks alone see
        it: the prompt, then the answer once `Stop` knows it.

        A run is what joins a conversation to its messages, and the transcript's real one
        only lands when the turn closes (the next prompt, or `SessionEnd`) — so without
        this a turn in flight would have a conversation and a chat log but no run to hang
        a model history from, and nothing to reuse until it ended. The tailer replaces
        this sketch wholesale with the full timeline at close; `prompt_id` is the run id
        both write under, so they are the same run throughout.

        Both hooks route here, and both read the prompt back off the inbound ledger row:
        `Stop` carries only `last_assistant_message`, never the prompt it answers.
        """
        if not event.prompt_id:
            return  # no per-turn key: nothing to write a run under
        thread = await self.session_thread(event.session_id)
        prompt = self.recorded_prompt(thread, event.prompt_id)
        if prompt is None or prompt.message_text is None:
            # The prompt hook never landed (Octomate came up mid-turn). Leave the turn
            # to the tailer, which rebuilds it from the transcript either way.
            return
        messages: list[PydanticModelMessage] = [
            # Dated to the prompt's own ledger row. `started_at` is read off the first
            # message, and both `Conversation.runs` and `.messages` order on it, so an
            # undated run sorts ahead of the whole history it belongs at the end of —
            # `ModelRequest.timestamp` defaults to None, unlike `ModelResponse`'s.
            ModelRequest(
                parts=[UserPromptPart(content=prompt.message_text)],
                timestamp=prompt.created_at,
            )
        ]
        if event.last_assistant_message:
            messages.append(
                ModelResponse(parts=[TextPart(content=event.last_assistant_message)])
            )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        await self.octomate.conversations.record_external_run(
            conversation,
            run_id=event.prompt_id,
            messages=messages,
            name=CLAUDE_NATIVE_ID,
            external_session_id=event.session_id,
        )

    def recorded_prompt(self, thread: Thread, prompt_id: str) -> ThreadMessage | None:
        """The turn's inbound ledger row, as `record_prompt` wrote it — the prompt's text
        and the time it arrived."""
        return next(
            (
                message
                for message in thread.messages
                if message.platform_message_id == prompt_id
                and message.direction == "inbound"
            ),
            None,
        )

    async def record_answer(self, event: ClaudeHookInput, answer: str) -> None:
        thread = await self.session_thread(event.session_id)
        if event.prompt_id and self.already_recorded(
            thread, event.prompt_id, "outbound"
        ):
            return
        await self.octomate.thread_manager.record_outbound(
            thread,
            agent_tentacle_id=CLAUDE_NATIVE_ID,
            segments=[MarkdownSegment(data={"text": answer})],
            platform_message_id=event.prompt_id or "",
        )

    def already_recorded(
        self, thread: Thread, prompt_id: str, direction: ThreadMessageDirection
    ) -> bool:
        """A hook can fire more than once (retries, a repeated `Stop`); the per-turn
        `prompt_id` + direction dedups so a re-fire is a no-op."""
        return any(
            message.platform_message_id == prompt_id and message.direction == direction
            for message in thread.messages
        )
