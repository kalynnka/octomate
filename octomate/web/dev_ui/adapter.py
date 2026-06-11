from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from fastapi.responses import StreamingResponse
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelRequest,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.ui.vercel_ai import VercelAIAdapter, VercelAIEventStream
from pydantic_ai.ui.vercel_ai.request_types import RequestData
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    DataChunk,
)

from octomate.capabilities.agent import Agent
from octomate.capabilities.events import (
    ActionBatchEvent,
    DisplayEvent,
    ResultSegmentEvent,
    ResultTextDeltaEvent,
)
from octomate.capabilities.react import (
    ReactDeps,
    ReactState,
    ResumeTurn,
    StartTurn,
    iter_react_graph_events,
)
from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import ConversationKey
from octomate.tentacles.agent.inkling.base import InklingOutput

logger = logging.getLogger(__name__)

# Synthetic part index for the re-materialized reply text; far above any real
# part index so the Vercel stream keys it as its own part.
REPLY_PART_INDEX = 1000


@dataclass
class GraphAdapter:
    """Drive react_graph and let pydantic-ai handle Vercel UI protocol details."""

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

        key = ConversationKey(
            channel_tentacle_id=self.channel_id,
            chat_type="private",
            chat_id=body.id,
            user_id="dev",
            thread_id="",
        )
        deferred = adapter.deferred_tool_results
        client_messages = adapter.sanitize_messages(
            adapter.messages,
            deferred_tool_results=deferred,
        )

        event_stream = VercelAIEventStream(
            body,
            sdk_version=self.SDK_VERSION,
        )
        return event_stream.streaming_response(
            event_stream.transform_stream(
                self.native_events(
                    conversation_key=key,
                    user_prompt=self.latest_user_prompt(client_messages),
                    deferred=deferred,
                ),
                on_complete=self.complete_chunks,
            )
        )

    async def native_events(
        self,
        *,
        conversation_key: ConversationKey,
        user_prompt: str | Sequence[UserContent] | None,
        deferred: DeferredToolResults | None,
    ) -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[InklingOutput]]:
        # The react stream is normalized (the reply arrives as ResultTextDeltaEvent /
        # ResultSegmentEvent, not raw text parts); the stock Vercel event stream only
        # speaks native AgentStreamEvents, so re-materialize the reply as a synthetic
        # text part and skip the octomate-only events it would silently drop. Proper
        # StreamEvents handling in dev_ui is UoW-12.
        events = iter_react_graph_events(
            self.start_node(user_prompt=user_prompt, deferred=deferred),
            state=ReactState(
                conversation_key=conversation_key,
                agent_tentacle_id=self.agent_id,
            ),
            deps=ReactDeps(
                agent=self.agent,
                conversation_manager=self.conversations,
                agent_deps=None,
            ),
        )
        reply_started = False
        try:
            async for event in events:
                match event:
                    case ResultTextDeltaEvent():
                        text = event.delta
                    case ResultSegmentEvent():
                        text = str(event.segment)
                    case FinalResult() | DisplayEvent() | ActionBatchEvent():
                        continue
                    case _:
                        yield event
                        continue
                if not text:
                    continue
                if reply_started:
                    yield PartDeltaEvent(
                        index=REPLY_PART_INDEX,
                        delta=TextPartDelta(content_delta=text),
                    )
                else:
                    reply_started = True
                    yield PartStartEvent(
                        index=REPLY_PART_INDEX, part=TextPart(content=text)
                    )
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
