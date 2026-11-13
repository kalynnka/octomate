"""Archive handoff around the upstream step-persistence lifecycle."""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from pathlib import Path

import anyio
from arcanus import Transmuter
from pydantic import UUID7
from pydantic_ai import AgentRunResult, RunContext
from pydantic_ai import Conversation as PydanticConversation
from pydantic_ai._conversation_marks import mark_stored, stored_boundary
from pydantic_ai.capabilities import AgentNode
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolReturnPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai_harness.step_persistence import StepPersistence as BaseStepPersistence

from octomate.managers.conversation import ConversationStore
from octomate.managers.steps import message_adapter
from octomate.managers.thread import ThreadManager
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.messages import ModelRequest
from octomate.types.permissions import AgentPermissionMode


@dataclass(kw_only=True)
class RunStore(ConversationStore):
    thread_manager: ThreadManager | None
    conversation: Conversation
    conversation_address: ChannelAddress
    source_thread_address: ChannelAddress | None
    source_thread_message_ids: list[UUID7]
    run_name: str
    cwd: Path | None
    binds_prompt_sources: bool
    permission_mode: AgentPermissionMode | None
    context_window: int | None = None  # Capacity of the last request's model.
    checkpoint: PydanticConversation | None = (
        None  # Last saved rewrite, allocated only on compaction.
    )

    async def record_run(
        self,
        run_id: str,
        conversation: PydanticConversation,
        *,
        model_name: str | None,
    ) -> None:
        if (
            self.checkpoint is not None
            and conversation.revision != self.checkpoint.revision
        ):
            conversation.revision = self.checkpoint.revision
            mark_stored(conversation, self.checkpoint.messages)
        async with self.manager.lock(self.conversation.key):
            messages = await self.record_conversation(conversation)
        if self.checkpoint is not None:
            self.checkpoint = conversation
        if not messages:
            return
        last_usage = next(
            (
                message.usage
                for message in reversed(messages)
                if isinstance(message, ModelResponse)
            ),
            None,
        )
        await self.manager.record_run_metadata(
            run_id,
            name=self.run_name,
            model_name=model_name,
            context_window=self.context_window,
            context_used=last_usage.input_tokens
            if last_usage is not None and last_usage.has_values()
            else None,
            permission_mode=self.permission_mode,
            cwd=self.cwd,
            returned_calls={
                part.tool_call_id
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            },
        )
        if not self.source_thread_message_ids or not self.binds_prompt_sources:
            return
        prompt_request = next(
            (
                message
                for message in messages
                if isinstance(message, ModelRequest) and message.role == "user"
            ),
            None,
        )
        if prompt_request is None:
            raise RuntimeError(
                "prompt-source bindings require a persisted user ModelRequest"
            )
        if self.thread_manager is None:
            raise RuntimeError("prompt-source bindings require a ThreadManager")
        await self.thread_manager.bind_messages(
            self.source_thread_message_ids,
            prompt_request.id,
            kind="request_source",
            run_id=run_id,
        )
        source_thread = await self.thread_manager.ensure(
            self.source_thread_address or self.conversation_address
        )
        await self.thread_manager.advance_prompt_cursor(
            source_thread,
            self.source_thread_message_ids[-1],
        )

        self.binds_prompt_sources = False

    async def record_rewrite[DepsT](
        self,
        ctx: RunContext[DepsT],
        before: list[ModelMessage],
        after: list[ModelMessage],
    ) -> None:
        if ctx.run_id is None:
            raise RuntimeError("compacting agent run has no run_id")
        previous = self.checkpoint or self.conversation
        conversation = PydanticConversation(
            conversation_id=previous.conversation_id,
            revision=previous.revision,
            messages=before,
            usage=copy(ctx.usage),
        )
        boundary = stored_boundary(previous)
        if boundary is not None:
            mark_stored(conversation, before[:boundary])
        await self.record_run(ctx.run_id, conversation, model_name=ctx.model.model_name)
        self.checkpoint = conversation
        await self.record_compaction(conversation, after=after, trigger="auto")


@dataclass(kw_only=True)
class StepPersistence[DepsT](BaseStepPersistence[DepsT]):
    archive: RunStore

    async def before_run(self, ctx: RunContext[DepsT]) -> None:
        # Upstream snapshots copy message values, not their ORM relationships.
        for index, message in enumerate(ctx.messages):
            if isinstance(message, Transmuter):
                ctx.messages[index] = message_adapter.validate_python(vars(message))
        await super().before_run(ctx)

    async def before_node_run(
        self, ctx: RunContext[DepsT], *, node: AgentNode[DepsT]
    ) -> AgentNode[DepsT]:
        # UserPromptNode rebinds history; refresh upstream's reference before the
        # first model request can fail without reaching an after-node boundary.
        self._stash_live_history(ctx)
        return node

    async def before_model_request(
        self, ctx: RunContext[DepsT], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        self.archive.context_window = request_context.model.context_window
        return await super().before_model_request(ctx, request_context)

    async def after_run[OutputT](
        self, ctx: RunContext[DepsT], *, result: AgentRunResult[OutputT]
    ) -> AgentRunResult[OutputT]:
        conversation = result.conversation
        await self.archive.record_run(
            result.run_id, conversation, model_name=result.response.model_name
        )
        if conversation.revision is not None:
            result._conversation_saved(conversation.revision)
        return await super().after_run(ctx, result=result)

    async def on_run_error(
        self, ctx: RunContext[DepsT], *, error: BaseException
    ) -> AgentRunResult[None]:
        try:
            return await super().on_run_error(ctx, error=error)
        except BaseException as raised:
            if raised is not error:
                raise
            if ctx.run_id is None:
                raise RuntimeError("failed agent run has no run_id") from error
            # Upstream captures the failure history, including incomplete tool cycles.
            with anyio.CancelScope(shield=True):
                snapshot = await self.store.latest_snapshot(
                    run_id=ctx.run_id, include_interrupted=True
                )
                if snapshot is None:
                    raise
                previous = self.archive.checkpoint or self.archive.conversation
                conversation = PydanticConversation(
                    messages=snapshot.messages,
                    usage=copy(ctx.usage),
                    conversation_id=previous.conversation_id,
                    revision=previous.revision,
                )
                # Snapshots hold value copies of the prefix already archived.
                if previous.new_message_index is not None:
                    mark_stored(
                        conversation,
                        snapshot.messages[: previous.new_message_index],
                    )
                await self.archive.record_run(
                    ctx.run_id,
                    conversation,
                    model_name=ctx.model.model_name,
                )
            raise
