from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import google.genai as genai  # type: ignore[import-untyped]
from google.genai._interactions import Stream
from google.genai.interactions import (
    ContentDelta,
    Interaction,
    InteractionCompleteEvent,
    InteractionSSEEvent,
    InteractionStartEvent,
    TextContent,
)

from octomate.config import DeepResearchConfig
from octomate.nerve import NerveStream, SendSegments
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.agent.research.common import (
    build_input,
    build_sources_footer,
    format_citations,
)

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)

POLL_INTERVAL = 10
MAX_RECONNECT_ATTEMPTS = 5


class DeepResearchTentacle(AgentTentacle):
    """Thorough multi-step research using the Gemini Deep Research agent.

    Autonomously plans, searches, reads, and synthesizes detailed cited reports.
    Tasks typically take 5-20 minutes and cost ~$2-5 per run.
    """

    client: genai.Client
    agent: str
    active_interactions: dict[SessionKey, str]

    def __init__(self, id: str, octopus: Octopus, config: DeepResearchConfig) -> None:
        super().__init__(id, octopus, description=config.description)
        client_kwargs: dict[str, Any] = {}
        if config.vertexai:
            client_kwargs["vertexai"] = True
            if config.project:
                client_kwargs["project"] = config.project
            if config.location:
                client_kwargs["location"] = config.location
        elif config.api_key:
            client_kwargs["api_key"] = config.api_key
        self.client = genai.Client(**client_kwargs)
        self.agent = config.agent
        self.active_interactions = {}

    def _create_stream(
        self, input_parts: list[dict[str, Any]]
    ) -> Stream[InteractionSSEEvent]:
        return self.client.interactions.create(
            input=cast(list, input_parts),
            agent=self.agent,
            background=True,
            stream=True,
            agent_config={
                "type": "deep-research",
                "thinking_summaries": "auto",
            },
        )

    def _reconnect_stream(
        self, interaction_id: str, last_event_id: str
    ) -> Stream[InteractionSSEEvent]:
        return self.client.interactions.get(
            id=interaction_id,
            stream=True,
            last_event_id=last_event_id,
        )

    async def interrupt(self, key: SessionKey) -> None:
        self.active_interactions.pop(key, None)

    async def run(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
        *,
        session_name: str = "",
        silent: bool = False,
    ) -> str | None:
        input_parts = build_input(contents)

        if silent:
            return await self._run_poll(key, input_parts)

        async with NerveStream(self.octopus.agent_nerve, key) as nerve_stream:
            await nerve_stream.set_status("Starting deep research...")

            interaction_id: str | None = None
            last_event_id: str | None = None
            collected_text = ""

            stream = await asyncio.to_thread(self._create_stream, input_parts)

            attempt = 0
            while attempt <= MAX_RECONNECT_ATTEMPTS:
                try:
                    for chunk in stream:
                        if key not in self.active_interactions and interaction_id:
                            logger.info("Deep research interrupted for [%s]", key)
                            return None

                        if isinstance(chunk, InteractionStartEvent):
                            iid = chunk.interaction.id
                            if iid:
                                interaction_id = iid
                                self.active_interactions[key] = iid

                        if chunk.event_id:
                            last_event_id = chunk.event_id

                        if isinstance(chunk, ContentDelta):
                            if chunk.delta.type == "text":
                                collected_text += chunk.delta.text
                                await nerve_stream.append(chunk.delta.text)
                            elif chunk.delta.type == "thought_summary":
                                thought = getattr(
                                    chunk.delta.content,
                                    "text",
                                    str(chunk.delta.content),
                                )
                                if thought:
                                    await nerve_stream.set_status(
                                        f"Researching: {thought[:120]}"
                                    )

                        elif isinstance(chunk, InteractionCompleteEvent):
                            break
                    break

                except Exception:
                    attempt += 1
                    if attempt > MAX_RECONNECT_ATTEMPTS or not interaction_id:
                        logger.exception("Deep research stream failed for [%s]", key)
                        raise
                    logger.warning(
                        "Deep research stream dropped for [%s], reconnecting (attempt %d)",
                        key,
                        attempt,
                    )
                    await asyncio.sleep(2)
                    stream = self._reconnect_stream(interaction_id, last_event_id or "")

            await nerve_stream.flush()

        self.active_interactions.pop(key, None)

        annotations = await self._collect_annotations(interaction_id)
        if annotations:
            sources = build_sources_footer(annotations)
            if sources:
                await self.octopus.agent_nerve.send(
                    SendSegments(
                        key=key,
                        segments=[MarkdownSegment(data={"text": sources})],
                    )
                )

        return None

    def _create_interaction(self, input_parts: list[dict[str, Any]]) -> Interaction:
        return self.client.interactions.create(
            input=cast(list, input_parts),
            agent=self.agent,
            background=True,
        )

    async def _run_poll(
        self, key: SessionKey, input_parts: list[dict[str, Any]]
    ) -> str | None:
        """Silent mode: poll for completion, no streaming to channel."""
        interaction = await asyncio.to_thread(self._create_interaction, input_parts)
        interaction_id: str = interaction.id
        self.active_interactions[key] = interaction_id

        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if key not in self.active_interactions:
                return None

            interaction = await asyncio.to_thread(
                self.client.interactions.get, interaction_id
            )
            if interaction.status == "completed":
                break
            elif interaction.status in ("failed", "cancelled"):
                logger.error(
                    "Deep research %s for [%s]: %s",
                    interaction.status,
                    key,
                    getattr(interaction, "error", "unknown"),
                )
                return f"Deep research {interaction.status}."

        self.active_interactions.pop(key, None)

        last_output = interaction.outputs[-1] if interaction.outputs else None
        text = last_output.text if isinstance(last_output, TextContent) else ""
        annotations = await self._collect_annotations(interaction_id)
        return format_citations(text, annotations)

    async def _collect_annotations(
        self, interaction_id: str | None
    ) -> list[dict[str, Any]]:
        if not interaction_id:
            return []
        try:
            interaction = await asyncio.to_thread(
                self.client.interactions.get, interaction_id
            )
            annotations: list[dict[str, Any]] = []
            for output in interaction.outputs or []:
                if isinstance(output, TextContent) and output.annotations:
                    annotations.extend(
                        a if isinstance(a, dict) else dict(a.__dict__)
                        for a in output.annotations
                    )
            return annotations
        except Exception:
            logger.debug(
                "Could not retrieve annotations for deep research %s",
                interaction_id,
                exc_info=True,
            )
            return []
