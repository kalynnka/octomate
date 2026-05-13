from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal
from uuid import uuid4

from fastapi.responses import StreamingResponse
from pydantic_ai import Agent, AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelRequest,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.ui import OnCompleteFunc
from pydantic_ai.ui.vercel_ai import VercelAIAdapter, VercelAIEventStream
from pydantic_ai.ui.vercel_ai.request_types import RequestData
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    DataChunk,
    TextDeltaChunk,
    TextEndChunk,
    TextStartChunk,
    ToolInputDeltaChunk,
    ToolInputAvailableChunk,
    ToolInputStartChunk,
)

from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.tentacles.agent.inkling.graph import (
    InklingDeps,
    InklingOutput,
    InklingState,
    ResumeTurn,
    StartTurn,
    iter_inkling_graph_events,
)

logger = logging.getLogger(__name__)


class OctomateVercelEventStream(VercelAIEventStream):
    async def transform_stream(
        self,
        stream: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[InklingOutput]],
        on_complete: OnCompleteFunc[BaseChunk] | None = None,
    ) -> AsyncIterator[BaseChunk]:
        async for chunk in super().transform_stream(stream, on_complete=on_complete):
            if isinstance(chunk, ToolInputStartChunk | ToolInputDeltaChunk):
                continue
            if (
                isinstance(chunk, ToolInputAvailableChunk)
                and chunk.tool_name == "final_result"
            ):
                continue
            yield chunk


@dataclass
class GraphAdapter:
    """Drive inkling_graph and let pydantic-ai handle Vercel UI protocol details."""

    SDK_VERSION: ClassVar[Literal[6]] = 6

    channel_id: str
    agent: Agent[None, InklingOutput]
    conversations: ConversationManager
    agent_id: str = "Inkling"

    async def handle_request(self, body: RequestData) -> StreamingResponse:
        adapter = VercelAIAdapter(
            agent=self.agent,
            run_input=body,
            sdk_version=self.SDK_VERSION,
        )

        conversation = await self.conversations.ensure(
            ConversationKey(
                channel_tentacle_id=self.channel_id,
                chat_type="private",
                chat_id=body.id,
                user_id="dev",
                thread_id="",
            ),
            agent_tentacle_id=self.agent_id,
        )
        deferred = adapter.deferred_tool_results
        client_messages = adapter.sanitize_messages(
            adapter.messages,
            deferred_tool_results=deferred,
        )

        event_stream = OctomateVercelEventStream(
            body,
            sdk_version=self.SDK_VERSION,
        )
        return event_stream.streaming_response(
            event_stream.transform_stream(
                self.native_events(
                    conversation=conversation,
                    history=list(conversation.messages),
                    user_prompt=self.latest_user_prompt(client_messages),
                    deferred=deferred,
                ),
                on_complete=self.complete_chunks,
            )
        )

    async def native_events(
        self,
        *,
        conversation: Conversation,
        history: Sequence[ModelMessage],
        user_prompt: str | Sequence[UserContent] | None,
        deferred: DeferredToolResults | None,
    ) -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[InklingOutput]]:
        try:
            async for event in iter_inkling_graph_events(
                self.start_node(user_prompt=user_prompt, deferred=deferred),
                state=InklingState(
                    conversation=conversation,
                    message_history=list(history),
                ),
                deps=InklingDeps(
                    agent=self.agent,
                    conversation_manager=self.conversations,
                ),
            ):
                yield event
        except Exception:
            logger.exception("DevUI graph stream failed")
            raise

    @staticmethod
    def start_node(
        *,
        user_prompt: str | Sequence[UserContent] | None,
        deferred: DeferredToolResults | None,
    ) -> StartTurn | ResumeTurn:
        if deferred is not None:
            return ResumeTurn(deferred_results=deferred)
        return StartTurn(user_prompt=user_prompt)

    @staticmethod
    def latest_user_prompt(
        messages: Sequence[ModelMessage],
    ) -> str | Sequence[UserContent] | None:
        for message in reversed(messages):
            if not isinstance(message, ModelRequest):
                continue
            contents = [
                part.content
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            if not contents:
                continue
            if len(contents) == 1:
                return contents[0]

            combined: list[UserContent] = []
            for content in contents:
                if isinstance(content, str):
                    combined.append(content)
                else:
                    combined.extend(content)
            return combined
        return None

    @staticmethod
    async def complete_chunks(
        result: AgentRunResult[InklingOutput],
    ) -> AsyncIterator[BaseChunk]:
        if isinstance(result.output, list):
            text = "\n\n".join(str(message) for message in result.output).strip()
            if not text:
                return
            text_id = str(uuid4())
            yield TextStartChunk(id=text_id)
            yield TextDeltaChunk(id=text_id, delta=text)
            yield TextEndChunk(id=text_id)
            return

        if not isinstance(result.output, DeferredToolRequests):
            return
        for call in result.output.calls:
            yield DataChunk(
                type="data-deferred-call",
                data={
                    "toolCallId": call.tool_call_id,
                    "toolName": call.tool_name,
                    "args": call.args_as_dict(),
                },
                transient=True,
            )
