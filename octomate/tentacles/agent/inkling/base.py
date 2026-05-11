"""Inkling: a `pydantic-ai`-backed `AgentTentacle`.

Owns one `Agent` and drives `inkling_graph` per turn. Each `ModelMessage`
produced inside the graph is emitted as a `StreamFrame` the moment it
appears (request before send, response after receive), so downstream
channels see history grow in real time. Per-part streaming events flow
alongside for live-delta UIs.
"""

from __future__ import annotations

import logging

from pydantic_ai import Agent
from pydantic_ai.messages import UserContent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.nerve import SendSegments, StreamFrame
from octomate.schemas.actions import AgentMessage
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import AgentInput, MessageEvent, ResumeEvent
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.inkling.graph import (
    InklingDeps,
    InklingOutput,
    InklingState,
    ResolveDeferred,
    ResumeTurn,
    StartTurn,
    inkling_graph,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.agent.inkling.resolver import DeferredResolver
from octomate.tentacles.agent.inkling.tools import inkling_toolset

logger = logging.getLogger(__name__)


def build_inkling_agent(
    model_id: str = "google:gemini-3-flash-preview",
) -> Agent[None, InklingOutput]:
    """Construct the bare pydantic-ai Agent. Kept as a factory so tests and
    the InklingTentacle share one builder; the `model_id` arg lets config
    override the default without touching the tentacle.
    """
    _, _, model_name = model_id.partition(":")
    model = GoogleModel(
        model_name or "gemini-3-flash-preview",
        provider=GoogleProvider(location="global"),
    )
    return Agent(
        model,
        deps_type=type(None),
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )


class NeverResolver(DeferredResolver):
    """The graph never reaches `ResolveDeferred` when DevUI drives the loop —
    deferred tools are surfaced as approval requests instead. This resolver
    asserts if the graph ever does call into it (defensive)."""

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        raise AssertionError(
            "InklingTentacle should intercept ResolveDeferred at the iter boundary"
        )


class InklingTentacle(AgentTentacle):
    """The inkling agent wired into the octopus event pipe.

    `__call__(conv, events)` is the single entrypoint. It dispatches by
    inspecting `events`: a `ResumeEvent` continues an in-flight deferred-tool
    turn; otherwise the `MessageEvent`s form a fresh user prompt.

    Output is emitted onto `octopus.agent_nerve` as:
      - `StreamFrame(payload={"model_message": <ModelMessage>})` for every
        request/response as it crosses a graph node — emitted in real time,
      - `StreamFrame(payload={"event": <pydantic_ai AgentStreamEvent>})` for
        each per-part model-streaming event,
      - `StreamFrame(payload={"deferred": <DeferredToolRequests>})` when the
        run aborts on a deferred tool,
      - `SendSegments(target_key=conv.key, segments=...)` for each final
        `AgentMessage` in the output,
      - `StreamFrame(frame_type="close")` to terminate the sink.
    """

    agent: Agent[None, InklingOutput]

    def __init__(
        self,
        id: str,
        *,
        agent: Agent[None, InklingOutput] | None = None,
        model_id: str = "google:gemini-3-flash-preview",
    ) -> None:
        super().__init__(id)
        self.agent = agent or build_inkling_agent(model_id)

    async def __call__(
        self,
        conversation: Conversation,
        events: list[AgentInput],
    ) -> None:
        resume = next((e for e in events if isinstance(e, ResumeEvent)), None)
        if resume is not None:
            if not isinstance(resume.payload, DeferredToolResults):
                logger.warning(
                    "InklingTentacle %s: ResumeEvent payload is %s, expected "
                    "DeferredToolResults; skipping",
                    self.id,
                    type(resume.payload).__name__,
                )
                return
            start_node = ResumeTurn(deferred_results=resume.payload)
        else:
            message_events = [e for e in events if isinstance(e, MessageEvent)]
            content: list[UserContent] = []
            for event in message_events:
                content.extend(event.to_content_parts())
            if not content:
                logger.info("InklingTentacle %s: empty batch; skipping", self.id)
                return
            start_node = StartTurn(user_prompt=content)

        async def emit_event(event: object) -> None:
            await self.octopus.emit(
                StreamFrame(
                    target_key=conversation.key,
                    frame_type="append",
                    payload={"event": event},
                )
            )

        async def emit_message(message: object) -> None:
            await self.octopus.emit(
                StreamFrame(
                    target_key=conversation.key,
                    frame_type="append",
                    payload={"model_message": message},
                )
            )

        deps = InklingDeps(
            agent=self.agent,
            resolver=NeverResolver(),
            event_sink=emit_event,
            message_sink=emit_message,
            conversation_manager=self.octopus.conversations,
        )
        state = InklingState(
            message_history=list(conversation.messages),
            conversation=conversation,
        )

        try:
            async with inkling_graph.iter(
                start_node,
                state=state,
                deps=deps,
            ) as run:
                async for node in run:
                    if isinstance(node, ResolveDeferred):
                        await self.octopus.emit(
                            StreamFrame(
                                target_key=conversation.key,
                                frame_type="append",
                                payload={"deferred": node.requests},
                            )
                        )
                        return
                if run.result is not None:
                    output = run.result.output
                    if isinstance(output, list):
                        for msg in output:
                            await self.octopus.emit(
                                SendSegments(
                                    target_key=conversation.key,
                                    segments=list(msg.segments),
                                )
                            )
        finally:
            await self.octopus.emit(
                StreamFrame(target_key=conversation.key, frame_type="close")
            )
