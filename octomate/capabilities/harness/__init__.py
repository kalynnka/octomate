"""Agent-run harness: Pydantic AI extensions + the react run-loop.

The machinery a capability runs inside, not a capability itself: the `Agent`
subclass, the `StreamEvents` catalog, the react loop, the deferred-tool protocols,
and the warm MCP toolset cache. The capabilities themselves live one level up in
`octomate.capabilities`; Octomate-specific orchestration (triage) and the concrete
agent (inkling) live in `tentacles.agent`.

The react loop is imported as the `octomate.capabilities.harness.react` submodule to
keep this package's import graph light.
"""

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.deferred import DeferredResolver, DeferredSuspender
from octomate.capabilities.harness.events import StreamEvents

__all__ = [
    "Agent",
    "DeferredResolver",
    "DeferredSuspender",
    "StreamEvents",
]
