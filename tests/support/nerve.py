"""Watching a thread's live events in a test, as the console does on its socket."""

from __future__ import annotations

import json

from anyio.abc import ObjectReceiveStream

from octomate.capabilities.harness.events import (
    ReflexAsleepEvent,
    wire_event_adapter,
)
from octomate.nerve import Impulse


async def settled(
    live: ObjectReceiveStream[Impulse],
) -> list[dict[str, object]]:
    """The kick's events as the console reads them off its socket, up to the
    reflex falling asleep."""
    events: list[dict[str, object]] = []
    async for impulse in live:
        if isinstance(impulse.event, ReflexAsleepEvent):
            return events
        events.append(
            json.loads(wire_event_adapter.dump_json(impulse.event, warnings=False))
        )
    raise AssertionError("the turn never settled")
