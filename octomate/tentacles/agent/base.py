from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Self

import logfire

from octomate.nerve import AgentResult, DismissPending
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import Tentacle

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class AgentTentacle(Tentacle):
    """A tentacle wrapping an agent. On-demand, not long-running.
    Borrows the calling ChannelTentacle's feelers and ink for user interaction.

    Only one run per session key at a time. If a new message arrives while the
    agent is running, the current run is cancelled and re-started with the new
    message. Pending tool calls, questions, and todos from the previous run are
    dismissed in parallel.
    """

    description: str = ""
    handover: bool = False
    _running_tasks: dict[SessionKey, asyncio.Task]

    def __init__(
        self,
        id: str,
        octopus: Octopus,
        description: str = "",
        *,
        handover: bool = False,
    ) -> None:
        super().__init__(id, octopus)
        self.description = description
        self.handover = handover
        self._running_tasks = {}

    @logfire.instrument("AgentTentacle {self.id} call [{key}]")
    async def __call__(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
        *,
        session_name: str = "",
        request_id: str = "",
        silent: bool = False,
    ) -> None:
        existing = self._running_tasks.get(key)
        if existing is not None and not existing.done():
            await self.interrupt(key)
            existing.cancel()
            asyncio.create_task(self._dismiss_pending(key))

        self._running_tasks[key] = asyncio.current_task()  # type: ignore[assignment]
        result_output = ""
        try:
            result_output = (
                await self.run(key, contents, session_name=session_name, silent=silent)
                or ""
            )
        except asyncio.CancelledError:
            logger.info("AgentTentacle %s: run cancelled for [%s]", self.id, key)
            raise
        except Exception:
            logger.exception("Error in agent tentacle %s [%s]", self.id, key)
            result_output = "Agent encountered an error."
        finally:
            if self._running_tasks.get(key) is asyncio.current_task():
                self._running_tasks.pop(key, None)
            if request_id:
                await self.octopus.agent_nerve.send(
                    AgentResult(key=key, request_id=request_id, output=result_output)
                )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def interrupt(self, key: SessionKey) -> None:
        """Send a graceful stop signal before hard-cancelling.

        Override in subclasses to e.g. send ClaudeSDKClient.interrupt().
        """

    @abstractmethod
    async def run(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
        *,
        session_name: str = "",
        silent: bool = False,
    ) -> str | None: ...

    async def _dismiss_pending(self, key: SessionKey) -> None:
        """Background task: expire all pending interactions and dismiss their cards."""
        try:
            await self.octopus.agent_nerve.send(DismissPending(key=key))
        except Exception:
            logger.exception(
                "AgentTentacle %s: error sending DismissPending for [%s]", self.id, key
            )
