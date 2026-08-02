"""Wiring for the reflex graph.

The nodes live one per module under `nodes/`; this is where they are assembled
into a runnable graph. Names the rest of the project imports from
`octomate.reflex.graph` are re-exported here so the split stays invisible to
callers.
"""

from __future__ import annotations

from pydantic_graph import Graph, GraphBuilder, TypeExpression

from octomate.reflex.nodes import (
    Awake,
    Handoff,
    React,
    ResumeDeferred,
    Route,
    Scheme,
    Teleport,
)
from octomate.reflex.state import (
    DeferredResult,
    ReflexDeps,
    ReflexEntryT,
    ReflexGraphResult,
    ReflexResult,
    ReflexState,
    ResponseTarget,
)
from octomate.schemas.triage import ResponseTargetMode, SummonDecision

__all__ = [
    "Awake",
    "DeferredResult",
    "Handoff",
    "React",
    "ReflexDeps",
    "ReflexEntryT",
    "ReflexGraphResult",
    "ReflexResult",
    "ReflexState",
    "ResponseTarget",
    "ResponseTargetMode",
    "ResumeDeferred",
    "Route",
    "Scheme",
    "SummonDecision",
    "Teleport",
    "build_reflex_graph",
    "reflex_graph",
]


def build_reflex_graph(
    entry: type[ReflexEntryT] = Awake,
) -> Graph[ReflexState, ReflexDeps, ReflexEntryT, ReflexGraphResult]:
    """Wire the reflex nodes into a runnable graph, entered at `entry`.

    Every edge comes from the nodes' own `run` return annotations, so the shape
    stays declared where the transition is written rather than in a second list
    here. A graph declares the one node it is entered at: a signal wakes the
    whole reflex, so that is `Awake`, and only a test wires the same nodes with
    a different door to exercise a stretch of them on its own.
    """
    builder = GraphBuilder(
        name="reflex",
        state_type=ReflexState,
        deps_type=ReflexDeps,
        input_type=entry,
        # `TypeExpression` is pydantic-graph's stand-in for a union in a
        # `type[...]` position — the result is one of two variants.
        output_type=TypeExpression[ReflexGraphResult],
    )
    builder.add(
        builder.edge_from(builder.start_node).to(entry),
        builder.node(Awake),
        builder.node(Route),
        builder.node(Handoff),
        builder.node(React),
        builder.node(Scheme),
        builder.node(Teleport),
        builder.node(ResumeDeferred),
    )
    # Entering anywhere but the top necessarily strands the nodes above it, so the
    # reachability check only means something for the real entry.
    return builder.build(validate_graph_structure=entry is Awake)


reflex_graph = build_reflex_graph()
