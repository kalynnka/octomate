"""Run event streams shared by agent providers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import TracebackType

from pydantic_ai import AgentRunResultEvent

from octomate.events import StreamEvents

# The events a react run streams: the normalized `StreamEvents` union (Pydantic AI
# passthrough + output/display events + a suspended run's deferred-action batch)
# plus the terminal result.
type ReactStreamEvent[ReactOutputT] = (
    StreamEvents[ReactOutputT] | AgentRunResultEvent[ReactOutputT]
)


class ReactEventStream[ReactOutputCoT]:
    """Deterministic-cleanup handle over a react event stream: entering the
    context yields the underlying generator, exiting closes it. Mirrors
    pydantic-ai's ``AgentEventStream``, but typed over ``ReactStreamEvent`` — a
    react run also streams octomate output/display/action-batch events."""

    # Private so the covariant type parameter has no mutable public surface.
    def __init__(
        self, generator: AsyncGenerator[ReactStreamEvent[ReactOutputCoT], None]
    ) -> None:
        self._generator = generator

    async def __aenter__(
        self,
    ) -> AsyncGenerator[ReactStreamEvent[ReactOutputCoT], None]:
        return self._generator

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._generator.aclose()
