"""The reflex graph: how an inbound signal becomes an agent turn, and the state,
deps and results that run carries. Re-exports what callers need."""

from octomate.reflex.graph import (
    Awake,
    DeferredResult,
    ReflexDeps,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
    ResponseTargetMode,
    SummonDecision,
    reflex_graph,
)
from octomate.reflex.suspender import ReflexSuspender

__all__ = [
    "Awake",
    "DeferredResult",
    "ReflexDeps",
    "ReflexGraphResult",
    "ReflexResult",
    "ReflexState",
    "ReflexSuspender",
    "ResponseTarget",
    "ResponseTargetMode",
    "SummonDecision",
    "reflex_graph",
]
