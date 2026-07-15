from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter, ValidationError
from pydantic_ai.messages import ModelMessage as PydanticModelMessage

from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelRequest
from octomate.schemas.runs import AgentRun
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.thread import (
    Thread,
    ThreadKey,
    ThreadMessage,
    ThreadMessageDirection,
)
from octomate.tentacles.agent.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.agent.claude.ingest import CLAUDE_NATIVE_ID, NATIVE_USER
from octomate.tentacles.agent.claude.transcript import (
    TranscriptAssistantLine,
    TranscriptLine,
    TranscriptUserLine,
    TranscriptUserMessage,
)

if TYPE_CHECKING:
    from octomate import Octomate

logger = logging.getLogger(__name__)

RestoreStatus = Literal["running", "done", "failed"]

# The transcript's message/tool-result lines, translated straight by the adapter.
TranscriptRecord = TranscriptAssistantLine | TranscriptUserLine

# Where Claude Code writes session transcripts, one dir per project (cwd slug).
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# One typed line at a time, tolerating kinds transcript.py does not model yet
# (`validate_json` raises on an unknown discriminator; we skip those lines).
transcript_line_adapter: TypeAdapter[TranscriptLine] = TypeAdapter(TranscriptLine)


def prompt_text(message: TranscriptUserMessage) -> str:
    """The plain text of a submitted prompt — the string itself, or the text of its
    text blocks (Claude Code pads a prompt with injected context blocks)."""
    content = message.content
    if isinstance(content, str):
        return content
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def stamp(messages: list[PydanticModelMessage], timestamp: datetime | None) -> None:
    """Date the messages a transcript record produced by that record's own clock —
    the accumulator stamps `now`, which is wrong for a run replayed off disk
    (`AgentRun.started_at` and ordering both read from these)."""
    if timestamp is None:
        return
    for message in messages:
        message.timestamp = timestamp


@dataclass
class RebuildTurn:
    """One user turn of a transcript: the opening prompt plus the typed assistant and
    tool-result lines that follow it, fed straight to the adapter."""

    prompt_id: str
    prompt_text: str
    started_at: datetime | None
    source: str | None = None  # the transcript `entrypoint` (claude-vscode / cli / …)
    records: list[TranscriptRecord] = field(default_factory=list)


@dataclass
class RestoreOutcome:
    session_id: str
    status: RestoreStatus
    recorded_run_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class RestoreState:
    status: RestoreStatus
    task: asyncio.Task[RestoreOutcome]


def read_lines(transcript_path: Path) -> list[TranscriptLine]:
    """The transcript's lines as transcript.py typed models, straight from the file
    bytes — pydantic parses the JSON, no `json.loads` step. Lines of a kind transcript.py
    does not model (or malformed) raise and are skipped."""
    lines: list[TranscriptLine] = []
    for raw in transcript_path.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            lines.append(transcript_line_adapter.validate_json(raw))
        except ValidationError:
            continue
    return lines


def split_turns(lines: list[TranscriptLine]) -> list[RebuildTurn]:
    """Split a transcript's typed lines into turns.

    A turn opens on a user line carrying `prompt_source` (the marker of a submitted
    prompt — tool-result user lines carry `prompt_id` too, so that alone can't tell
    them apart). Everything up to the next prompt belongs to the open turn. Sub-agent
    (`is_sidechain`) lines are skipped; non-message line kinds are ignored.
    """
    turns: list[RebuildTurn] = []
    for line in lines:
        if isinstance(line, TranscriptUserLine):
            if line.is_sidechain:
                continue
            if line.prompt_source is not None:
                turns.append(
                    RebuildTurn(
                        prompt_id=line.prompt_id,
                        prompt_text=prompt_text(line.message),
                        started_at=line.timestamp,
                        source=line.entrypoint,
                    )
                )
            elif turns:
                turns[-1].records.append(line)
        elif isinstance(line, TranscriptAssistantLine):
            if line.is_sidechain:
                continue
            if turns:
                turns[-1].records.append(line)
    return turns


class ClaudeSessionRestore:
    """Rebuilds a native Claude session's full model timeline from its transcript.

    A live session's hooks only persist the human ledger (prompt/answer) — the model
    messages, with thinking blocks and token usage, live only in the on-disk
    transcript. This reads that transcript and materializes each turn as an
    `AgentRun` of pydantic-ai `ModelMessage`s, reusing the `claude` tentacle's own
    `ClaudeRunAccumulator`, then binds the human-ledger rows to them (creating any the
    live pipe never wrote, so a session Octomate never watched still restores whole).

    Recording is keyed on the turn's `prompt_id` (its run id); a turn already present
    is skipped, so a rebuild is idempotent and can run repeatedly as a session grows.

    A per-session status tracker doubles as a dedup guard: a rebuild in flight is
    shared, so a web open landing while a fire-and-forget rebuild runs joins it rather
    than starting a second.
    """

    def __init__(self, octomate: Octomate) -> None:
        self.octomate = octomate
        self.states: dict[str, RestoreState] = {}

    def status(self, session_id: str) -> RestoreStatus | None:
        state = self.states.get(session_id)
        return state.status if state is not None else None

    def locate_transcript(self, session_id: str) -> Path | None:
        """The session's transcript on local disk, newest if a slug moved. A caller
        with only a session id (a web open) uses this; a hook already carries the
        path."""
        matches = sorted(
            CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    async def restore(self, session_id: str, transcript_path: Path) -> RestoreOutcome:
        """Rebuild the session's model timeline, joining an in-flight rebuild if one is
        already running (the dedup guard). Awaited by a caller that wants the result —
        a web open reviewing the session."""
        return await self.task(session_id, transcript_path)

    def task(
        self, session_id: str, transcript_path: Path
    ) -> asyncio.Task[RestoreOutcome]:
        """The rebuild task for the session: the in-flight one if a rebuild is already
        running, else a freshly started one. This is the dedup guard — one task per
        session at a time."""
        state = self.states.get(session_id)
        if state is not None and not state.task.done():
            return state.task
        task = asyncio.create_task(self.run(session_id, transcript_path))
        self.states[session_id] = RestoreState(status="running", task=task)
        return task

    async def run(self, session_id: str, transcript_path: Path) -> RestoreOutcome:
        # The task boundary: own its own materia context (a fire-and-forget task
        # outlives the request that spawned it) and record the outcome as status
        # rather than an unretrieved background exception.
        try:
            with sqlalchemy_materia():
                run_ids = await self.rebuild(session_id, transcript_path)
        except Exception as exc:
            self.mark(session_id, "failed")
            logger.exception("Restore of native Claude session %s failed", session_id)
            return RestoreOutcome(session_id, "failed", error=str(exc))
        self.mark(session_id, "done")
        logger.info(
            "Restored %d turn(s) of native Claude session %s",
            len(run_ids),
            session_id,
        )
        return RestoreOutcome(session_id, "done", run_ids)

    def mark(self, session_id: str, status: RestoreStatus) -> None:
        state = self.states.get(session_id)
        if state is not None:
            state.status = status

    async def rebuild(self, session_id: str, transcript_path: Path) -> list[str]:
        thread = await self.octomate.thread_manager.ensure(
            ThreadKey(CLAUDE_NATIVE_ID, "private", session_id, "")
        )
        conversation = await self.octomate.conversations.ensure(
            thread.id, agent_tentacle_id=CLAUDE_NATIVE_ID
        )
        recorded = {run.id for run in conversation.runs}
        run_ids: list[str] = []
        for turn in split_turns(read_lines(transcript_path)):
            if turn.prompt_id in recorded:
                continue
            run = await self.record_turn(conversation, thread, turn, session_id)
            if run is not None:
                run_ids.append(run.id)
        return run_ids

    async def record_turn(
        self,
        conversation: Conversation,
        thread: Thread,
        turn: RebuildTurn,
        session_id: str,
    ) -> AgentRun | None:
        accumulator = ClaudeRunAccumulator()
        accumulator.begin(turn.prompt_text)
        stamp(accumulator.messages, turn.started_at)
        for line in turn.records:
            written = len(accumulator.messages)
            # The adapter consumes transcript lines directly (converting to the SDK
            # shape it also handles live). `consume` yields the stream events a channel
            # would render; there is no channel behind a replay, only the accumulated
            # messages are wanted.
            for _ in accumulator.consume(line):
                pass
            stamp(accumulator.messages[written:], line.timestamp)

        run = await self.octomate.conversations.record_external_run(
            conversation,
            run_id=turn.prompt_id,
            messages=accumulator.messages,
            name=CLAUDE_NATIVE_ID,
            external_session_id=session_id,
            source=turn.source,
        )
        if run is None:
            return None
        await self.bind_human_ledger(
            thread, turn, run, accumulator.result_text, session_id
        )
        return run

    async def bind_human_ledger(
        self,
        thread: Thread,
        turn: RebuildTurn,
        run: AgentRun,
        answer: str,
        session_id: str,
    ) -> None:
        """Cross-reference the human chat ledger to the rebuilt model timeline, keyed
        by `prompt_id`. Rows the live pipe already wrote are reused; any it never wrote
        (Octomate was down during the session) are created here so restore stands
        alone."""
        thread_manager = self.octomate.thread_manager
        prompt_request = next(
            (
                message
                for message in run.messages
                if isinstance(message, ModelRequest) and message.role == "user"
            ),
            None,
        )
        if prompt_request is not None:
            inbound = self.existing_message(thread, turn.prompt_id, "inbound")
            if inbound is None:
                inbound = await thread_manager.record_inbound(
                    MessageEvent(
                        tentacle_id=CLAUDE_NATIVE_ID,
                        message_id=turn.prompt_id,
                        chat_id=session_id,
                        chat_type="private",
                        user_id=NATIVE_USER.user_id,
                        sender=NATIVE_USER,
                        segments=[TextSegment(data={"text": turn.prompt_text})],
                    )
                )
            await thread_manager.bind_messages(
                [inbound.id], prompt_request.id, kind="request_source", run_id=run.id
            )
        if answer:
            outbound = self.existing_message(thread, turn.prompt_id, "outbound")
            if outbound is None:
                outbound = await thread_manager.record_outbound(
                    thread,
                    agent_tentacle_id=CLAUDE_NATIVE_ID,
                    segments=[MarkdownSegment(data={"text": answer})],
                    platform_message_id=turn.prompt_id,
                )
            await thread_manager.bind_assistant_replies([outbound.id], run_id=run.id)

    def existing_message(
        self, thread: Thread, prompt_id: str, direction: ThreadMessageDirection
    ) -> ThreadMessage | None:
        return next(
            (
                message
                for message in thread.messages
                if message.platform_message_id == prompt_id
                and message.direction == direction
            ),
            None,
        )
