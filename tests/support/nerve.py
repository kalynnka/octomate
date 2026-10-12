"""Watching a thread's live events in a test, as the console does on its socket."""

from __future__ import annotations

import json

from anyio.streams.memory import MemoryObjectReceiveStream

from octomate.capabilities.harness.events import (
    ReflexAsleepEvent,
    WireEvent,
    wire_event_adapter,
)


async def settled(
    live: MemoryObjectReceiveStream[WireEvent],
) -> list[dict[str, object]]:
    """The kick's events as the console reads them off its socket, up to the
    reflex falling asleep."""
    events: list[dict[str, object]] = []
    async for event in live:
        if isinstance(event, ReflexAsleepEvent):
            return events
        events.append(json.loads(wire_event_adapter.dump_json(event, warnings=False)))
    raise AssertionError("the turn never settled")
