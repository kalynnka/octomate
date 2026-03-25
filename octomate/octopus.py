from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from pydantic_ai import Agent, AgentRunResult, DeferredToolResults
from pydantic_ai.tools import DeferredToolRequests

from octomate.agents.base import SessionContext, SummonRequest
from octomate.agents.manager import SKILL_METADATA_KEY, SkillManager
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.store import InteractionStore
from octomate.tentacles.base import AgentTentacle, ChannelTentacle, SendTarget
from octomate.transmuters import sqlalchemy_materia

logger = logging.getLogger(__name__)


@dataclass
class ThreadSession:
    """Tracks an agent-tentacle session bound to a platform sub-thread.

    Registered when a summon is issued; updated with the Claude Code
    ``session_id`` once the first run completes so subsequent messages
    in the same thread resume the same context.
    """

    tentacle_tag: str
    session_id: str | None
    target: SendTarget


class Octopus:
    store: InteractionStore
    skill_manager: SkillManager | None
    tentacles: dict[str, ChannelTentacle]
    agent_tentacles: dict[str, AgentTentacle]
    _nerve_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _nerve_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]
    thread_sessions: dict[tuple[str, str], ThreadSession]

    def __init__(
        self,
        skill_manager: SkillManager | None = None,
        buffer_size: int = 64,
    ) -> None:
        self.tentacles = {}
        self.agent_tentacles = {}
        self.store = InteractionStore()
        self.skill_manager = skill_manager
        self._nerve_send, self._nerve_receive = object_stream(buffer_size)
        self.thread_sessions: dict[tuple[str, str], ThreadSession] = {}

    async def activate(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                stack.enter_context(sqlalchemy_materia)
                if self.skill_manager:
                    await stack.enter_async_context(self.skill_manager)
                async with asyncio.TaskGroup() as tg:
                    for name, tentacle in self.tentacles.items():
                        tg.create_task(tentacle.activate(), name=f"tentacle:{name}")
                    tg.create_task(self.rolling())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            for tentacle in self.tentacles.values():
                await tentacle.deactivate()

    def connect(self, tentacle: ChannelTentacle) -> None:
        if tentacle.tag in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.tag!r} already connected")
        self.tentacles[tentacle.tag] = tentacle
        logger.info("Connected tentacle: %s", tentacle.tag)

    def graft(self, tentacle: AgentTentacle) -> None:
        if tentacle.tag in self.agent_tentacles:
            raise ValueError(f"Agent tentacle {tentacle.tag!r} already grafted")
        self.agent_tentacles[tentacle.tag] = tentacle
        logger.info("Grafted agent tentacle: %s", tentacle.tag)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    # ------------------------------------------------------------------
    # Thread-session registry
    # ------------------------------------------------------------------

    def register_thread_session(
        self,
        tentacle_id: str,
        thread_id: str,
        tentacle_tag: str,
        target: SendTarget,
    ) -> None:
        """Register a sub-thread as owned by the given agent tentacle.

        Called at summon time so that follow-up messages in the thread can
        be routed back without going through Flick again.
        """
        self.thread_sessions[(tentacle_id, thread_id)] = ThreadSession(
            tentacle_tag=tentacle_tag,
            session_id=None,
            target=target,
        )
        logger.debug(
            "Thread session registered: %s / %s → %s",
            tentacle_id,
            thread_id,
            tentacle_tag,
        )

    def update_thread_session_id(
        self, tentacle_id: str, thread_id: str, session_id: str
    ) -> None:
        """Persist the Claude Code session_id once a run completes.

        Subsequent turns in the same sub-thread will pass this id to
        ClaudeAgentOptions.resume so the session carries forward.
        """
        entry = self.thread_sessions.get((tentacle_id, thread_id))
        if entry is not None:
            entry.session_id = session_id
            logger.debug(
                "Thread session updated: %s / %s  session=%s",
                tentacle_id,
                thread_id,
                session_id,
            )

    def get_thread_session(
        self, tentacle_id: str, thread_id: str
    ) -> ThreadSession | None:
        """Return the ThreadSession registered for a sub-thread, or None."""
        return self.thread_sessions.get((tentacle_id, thread_id))

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self._nerve_send.send((key, batch))

    async def rolling(self) -> None:
        async with asyncio.TaskGroup() as tg, self._nerve_receive:
            async for key, batch in self._nerve_receive:
                tentacle = self.tentacles[key.tentacle_id]
                tg.create_task(tentacle(key, batch))

    async def resolve(
        self,
        agent: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        result: AgentRunResult[list[AgentMessage] | DeferredToolRequests],
        key: SessionKey,
        deps: SessionContext,
        target: SendTarget,
        tentacle: ChannelTentacle,
    ) -> AgentRunResult[list[AgentMessage]] | SummonRequest | None:
        """Resolve deferred tool requests until the agent produces final messages.

        Returns the final result with list[AgentMessage] output,
        a SummonRequest if the agent summoned an agent tentacle,
        or None if resolution failed.
        """
        while isinstance(result.output, DeferredToolRequests):
            # Check for summon — abort immediately, extract args from the deferred call
            summon_call = next(
                (c for c in result.output.calls if c.tool_name == "summon"), None
            )
            if summon_call:
                args = summon_call.args_as_dict()
                return SummonRequest(
                    tentacle_tag=args.get("tentacle_tag", ""),
                    summary=args.get("summary", ""),
                    user_prefer=args.get("user_prefer", ""),
                    language=args.get("language", ""),
                )

            deferred = DeferredToolResults()

            for call in result.output.calls:
                if call.tool_name == "ask_user":
                    args = call.args_as_dict()
                    resp = await tentacle.feelers.questions.ask_question(
                        target, args.get("question", ""), args.get("options")
                    )
                    deferred.calls[call.tool_call_id] = (
                        resp.answer if resp else "(no response)"
                    )

            for call in result.output.approvals:
                tool_meta = result.output.metadata.get(call.tool_call_id, {})
                action, future = self.store.create_confirmation(
                    session_key=key,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    args=call.args_as_dict(),
                    title=tool_meta.get("description", call.tool_name),
                    description=tool_meta.get("description", ""),
                    skill=tool_meta.get(SKILL_METADATA_KEY, ""),
                    approvers=tool_meta.get("approvers"),
                )

                sent = await tentacle.send_confirmation(target, action)
                if not sent:
                    self.store.expire_confirmation(action.confirmation_id)
                    deferred.approvals[call.tool_call_id] = False
                    continue

                try:
                    approved = await asyncio.wait_for(
                        future, timeout=self.store.timeout
                    )
                except TimeoutError:
                    self.store.expire_confirmation(action.confirmation_id)
                    approved = False

                deferred.approvals[call.tool_call_id] = approved

            result = await agent.run(
                message_history=result.all_messages(),
                deferred_tool_results=deferred,
                deps=deps,
                toolsets=tentacle.toolsets,
            )

        return result  # type: ignore[return-value]
