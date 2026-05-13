from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, ClassVar

from fastapi.responses import StreamingResponse
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import (
    DeferredToolApprovalResult,
    DeferredToolRequests,
    DeferredToolResults,
    ToolDenied,
)
from pydantic_ai.ui.vercel_ai._event_stream import (
    VERCEL_AI_DSP_HEADERS,
    VercelAIEventStream,
)
from pydantic_ai.ui.vercel_ai._utils import iter_tool_approval_responses
from pydantic_ai.ui.vercel_ai.request_types import (
    RequestData,
    SubmitMessage,
    TextUIPart,
    UIMessage,
)
from pydantic_ai.ui.vercel_ai.response_types import BaseChunk, DataChunk

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.tentacles.agent.inkling.graph import (
    InklingDeps,
    InklingOutput,
    InklingState,
    InklingStreamEvent,
    ResolveDeferred,
    ResumeTurn,
    StartTurn,
    inkling_graph,
)
from octomate.tentacles.agent.inkling.resolver import DeferredResolver

logger = logging.getLogger(__name__)


class NeverResolver(DeferredResolver):
    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        raise AssertionError("DevUI intercepts ResolveDeferred before resolver use")


class StreamEnd: ...


STREAM_END = StreamEnd()


@dataclass
class StreamError:
    error: Exception


class OctomateVercelEventStream(VercelAIEventStream):
    async def handle_event(self, event):
        if isinstance(event, BaseChunk):
            yield event
            return
        async for chunk in super().handle_event(event):
            yield chunk


@dataclass
class GraphAdapter:
    """Drive inkling_graph and let pydantic-ai emit Vercel protocol chunks."""

    SDK_VERSION: ClassVar[int] = 6

    channel_id: str
    agent: Agent[None, InklingOutput]
    conversations: ConversationManager
    agent_id: str = "Inkling"

    async def handle_request(self, body: RequestData) -> StreamingResponse:
        if not isinstance(body, SubmitMessage):
            return StreamingResponse(
                self._noop_stream(body),
                media_type="text/event-stream",
                headers=VERCEL_AI_DSP_HEADERS,
            )

        chat_id = body.id
        user_text = self._extract_latest_user_text(body.messages)
        deferred = self._extract_deferred_results(body.messages)

        conversation = await self.conversations.ensure(
            ConversationKey(
                channel_tentacle_id=self.channel_id,
                chat_type="private",
                chat_id=chat_id,
                user_id="dev",
                thread_id="",
            ),
            agent_tentacle_id=self.agent_id,
        )
        history: list[ModelMessage] = list(conversation.messages)

        return StreamingResponse(
            self._stream(
                body=body,
                conversation=conversation,
                history=history,
                user_text=user_text,
                deferred=deferred,
            ),
            media_type="text/event-stream",
            headers=VERCEL_AI_DSP_HEADERS,
        )

    async def _stream(
        self,
        *,
        body: RequestData,
        conversation: Conversation,
        history: list[ModelMessage],
        user_text: str,
        deferred: DeferredToolResults | None,
    ):
        queue: asyncio.Queue[InklingStreamEvent | BaseChunk | StreamEnd | StreamError]
        queue = asyncio.Queue()

        async def sink(event: InklingStreamEvent) -> None:
            await queue.put(event)

        deps = InklingDeps(
            agent=self.agent,
            resolver=NeverResolver(),
            event_sink=sink,
            conversation_manager=self.conversations,
        )
        state = InklingState(
            message_history=list(history),
            conversation=conversation,
        )
        start_node = (
            ResumeTurn(deferred_results=deferred)
            if deferred is not None
            else StartTurn(user_prompt=user_text)
        )

        async def driver() -> None:
            try:
                async with inkling_graph.iter(
                    start_node,
                    state=state,
                    deps=deps,
                ) as run:
                    async for node in run:
                        if isinstance(node, ResolveDeferred):
                            for chunk in self._deferred_call_chunks(node.requests):
                                await queue.put(chunk)
                            return
            except Exception as exc:
                logger.exception("DevUI graph driver failed")
                await queue.put(StreamError(exc))
            finally:
                await queue.put(STREAM_END)

        async def native_events():
            while True:
                item = await queue.get()
                if isinstance(item, StreamEnd):
                    break
                if isinstance(item, StreamError):
                    raise item.error
                yield item

        task = asyncio.create_task(driver())
        event_stream = OctomateVercelEventStream(body, sdk_version=self.SDK_VERSION)

        try:
            async for chunk in event_stream.transform_stream(native_events()):
                yield event_stream.encode_event(chunk).encode()
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _noop_stream(self, body: RequestData):
        async def empty_events():
            if False:
                yield None

        event_stream = OctomateVercelEventStream(body, sdk_version=self.SDK_VERSION)
        async for chunk in event_stream.transform_stream(empty_events()):
            yield event_stream.encode_event(chunk).encode()

    @staticmethod
    def _extract_latest_user_text(messages: list[UIMessage]) -> str:
        for msg in reversed(messages):
            if msg.role != "user":
                continue
            text_parts = [p.text for p in msg.parts if isinstance(p, TextUIPart)]
            if text_parts:
                return "\n".join(text_parts)
        return ""

    @staticmethod
    def _extract_deferred_results(
        messages: list[UIMessage],
    ) -> DeferredToolResults | None:
        approvals: dict[str, DeferredToolApprovalResult | bool] = {}
        for tool_call_id, approval in iter_tool_approval_responses(messages):
            if approval.approved:
                approvals[tool_call_id] = True
            elif approval.reason:
                approvals[tool_call_id] = ToolDenied(message=approval.reason)
            else:
                approvals[tool_call_id] = False
        if not approvals:
            return None
        return DeferredToolResults(approvals=approvals)

    @staticmethod
    def _deferred_call_chunks(requests: DeferredToolRequests) -> list[DataChunk]:
        chunks: list[DataChunk] = []
        for call in requests.calls:
            args: Any
            if hasattr(call, "args_as_dict"):
                args = call.args_as_dict()
            else:
                args = call.args
            chunks.append(
                DataChunk(
                    type="data-deferred-call",
                    data={
                        "toolCallId": call.tool_call_id,
                        "toolName": call.tool_name,
                        "args": args,
                    },
                    transient=True,
                )
            )
        return chunks
