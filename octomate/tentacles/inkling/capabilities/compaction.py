"""Persist accepted upstream compactions without changing their strategy."""

from pydantic_ai import RunContext
from pydantic_ai.capabilities import CapabilityOrdering
from pydantic_ai.models import ModelRequestContext
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
        compacted = await super().before_model_request(ctx, request_context)
        if (
            compacted is request_context
            or compacted.messages == request_context.messages
        ):
            return compacted
        try:
            persistence = ctx.capabilities["step_persistence"]
            if not isinstance(persistence, StepPersistence):
                raise TypeError(
                    "SummarizingCompaction requires Inkling's StepPersistence capability"
                )
            await persistence.archive.record_rewrite(
                ctx, request_context.messages, compacted.messages
            )
        except Exception:
            ctx.messages[:] = request_context.messages
            raise
        return compacted
