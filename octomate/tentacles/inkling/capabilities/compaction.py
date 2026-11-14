"""Persist accepted upstream compactions without changing their strategy."""

from dataclasses import replace

from pydantic_ai import RunContext
from pydantic_ai.capabilities import CapabilityOrdering
from pydantic_ai.models import ModelRequestContext
from pydantic_ai_harness.compaction import (
    CompactOnRequest as BaseCompactOnRequest,
)
from pydantic_ai_harness.compaction import (
    SummarizingCompaction as BaseSummarizingCompaction,
)

from octomate.tentacles.inkling.capabilities.persistence import StepPersistence


class SummarizingCompaction[DepsT](BaseSummarizingCompaction[DepsT]):
    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(requires=[StepPersistence])

    async def before_model_request(
        self, ctx: RunContext[DepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        return await record_compaction(
            ctx,
            request_context,
            await super().before_model_request(ctx, request_context),
        )


class CompactOnRequest[DepsT](BaseCompactOnRequest[DepsT]):
    def get_ordering(self) -> CapabilityOrdering:
        return replace(super().get_ordering(), requires=[StepPersistence])

    async def before_model_request(
        self, ctx: RunContext[DepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        return await record_compaction(
            ctx,
            request_context,
            await super().before_model_request(ctx, request_context),
        )


async def record_compaction[DepsT](
    ctx: RunContext[DepsT],
    before: ModelRequestContext,
    after: ModelRequestContext,
) -> ModelRequestContext:
    """Archive accepted rewrites, restoring live context if persistence fails."""
    if after is before or after.messages == before.messages:
        return after
    try:
        persistence = ctx.capabilities["step_persistence"]
        if not isinstance(persistence, StepPersistence):
            raise TypeError("Compaction requires Inkling's StepPersistence capability")
        await persistence.archive.record_rewrite(ctx, before.messages, after.messages)
    except Exception:
        ctx.messages[:] = before.messages
        raise
    return after
