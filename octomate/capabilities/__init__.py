"""Agent-run harness: Pydantic AI extensions + the react run-loop.

This package holds the reusable agent-run machinery (the `Agent` subclass, the
`StreamEvents` catalog, the react loop, deferred-tool protocols, and capabilities).
Octomate-specific orchestration (triage) and the concrete agent (inkling) live in
`tentacles.agent`.

The react loop is imported as the `octomate.capabilities.react` submodule to keep
this package's import graph light.
"""

from octomate.capabilities.agent import Agent
from octomate.capabilities.deferred import DeferredResolver, DeferredSuspender
from octomate.capabilities.events import (
    ActionRequestEvent,
    ApprovalRequestEvent,
    AskQuestionEvent,
    DisplayEvent,
    OutputDeltaEvent,
    StreamEvents,
    TodoItem,
    TodoListEvent,
)

__all__ = [
    "Agent",
    "DeferredResolver",
    "DeferredSuspender",
    "StreamEvents",
    "OutputDeltaEvent",
    "DisplayEvent",
    "TodoItem",
    "TodoListEvent",
    "ActionRequestEvent",
    "AskQuestionEvent",
    "ApprovalRequestEvent",
]
