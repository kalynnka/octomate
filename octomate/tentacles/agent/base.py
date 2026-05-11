from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from octomate.schemas.conversation import Conversation
from octomate.schemas.events import AgentInput
from octomate.tentacles.base import Tentacle


class AgentTentacle(Tentacle, ABC):
    """Base for grafted agents.

    An agent receives a `MessageBatch` (already routed by the octopus to the
    agent named on the conversation's `agent_tentacle_id`) and emits its
    output back through `self.octopus.emit(...)`. Streaming output goes via
    `StreamFrame`; the final discrete message goes via `SendSegments`.

    Subclasses own their per-conversation state internally (e.g. the
    pydantic-graph state machine for Inkling). `__aenter__` / `__aexit__`
    hook in for resource setup/teardown; the octopus enters them in its
    `run()` AsyncExitStack.
    """

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    @abstractmethod
    async def __call__(
        self,
        conversation: Conversation,
        events: list[AgentInput],
    ) -> None:
        """Drive one turn for the given conversation.

        `events` is a heterogeneous batch — `MessageEvent`s for fresh user
        input and/or a `ResumeEvent` for a deferred-tool resolution. The
        agent dispatches on the inbound mix; one entrypoint covers both
        fresh-turn and resume paths.

        Output is emitted via `self.octopus.emit(signal)` — typically a
        sequence of `StreamFrame`s for streaming text followed by a
        terminal `StreamFrame(frame_type='close')` and/or a `SendSegments`
        for the final reply.
        """
