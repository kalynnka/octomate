from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent
from pydantic_ai.builtin_tools import WebFetchTool, WebSearchTool
from pydantic_ai.messages import (
    BuiltinToolReturnPart,
    ModelResponse,
    PartDeltaEvent,
    TextPartDelta,
    UserContent,
)
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.run import AgentRunResultEvent

from octomate.config import FastResearchConfig
from octomate.nerve import NerveStream, SendSegments
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    FileSegment,
    ImageSegment,
    MarkdownSegment,
)
from octomate.schemas.session import SessionKey
from octomate.tentacles.agent.base import AgentTentacle

if TYPE_CHECKING:
    from pydantic_ai import AgentRunResult

    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class FastResearchTentacle(AgentTentacle):
    """Quick web-grounded research using a Gemini model with google_search + url_context.

    Produces answers in seconds with inline source citations. Suitable for
    factual lookups, news summaries, and quick question answering.
    """

    agent: Agent[None, str]

    def __init__(self, id: str, octopus: Octopus, config: FastResearchConfig) -> None:
        super().__init__(id, octopus, description=config.description)
        kwargs: dict[str, Any] = {}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.vertexai:
            kwargs["vertexai"] = True
            if config.project:
                kwargs["project"] = config.project
            if config.location:
                kwargs["location"] = config.location
        elif config.api_key:
            kwargs["api_key"] = config.api_key

        model = GoogleModel(config.model, provider=GoogleProvider(**kwargs))
        self.agent = Agent(
            model,
            output_type=str,
            builtin_tools=[WebSearchTool(), WebFetchTool()],
        )

    async def run(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
        *,
        session_name: str = "",
        silent: bool = False,
    ) -> str | None:
        user_prompt = _build_user_prompt(contents)

        if silent:
            result = await self.agent.run(user_prompt=user_prompt)
            return result.output

        result: AgentRunResult[str] | None = None
        async with NerveStream(self.octopus.agent_nerve, key) as nerve_stream:
            await nerve_stream.set_status("Researching...")
            async for event in self.agent.run_stream_events(user_prompt=user_prompt):
                if isinstance(event, AgentRunResultEvent):
                    result = event.result
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta, TextPartDelta
                ):
                    await nerve_stream.append(event.delta.content_delta)
            await nerve_stream.flush()

        if result:
            footer = _build_sources_footer(result)
            if footer:
                await self.octopus.agent_nerve.send(
                    SendSegments(
                        key=key,
                        segments=[MarkdownSegment(data={"text": footer})],
                    )
                )

        return None


def _build_user_prompt(contents: list[MessageEvent]) -> str | list[UserContent]:
    from pydantic_ai.messages import DocumentUrl, ImageUrl

    texts: list[str] = []
    media: list[UserContent] = []
    for event in contents:
        texts.extend(event.text_parts())
        for seg in event.segments:
            if isinstance(seg, ImageSegment) and seg.data.url:
                media.append(ImageUrl(url=seg.data.url))
            elif isinstance(seg, FileSegment) and seg.data.file:
                media.append(DocumentUrl(url=seg.data.file))

    combined = "\n".join(texts).strip()
    if not media:
        return combined or ""
    parts: list[UserContent] = []
    if combined:
        parts.append(combined)
    parts.extend(media)
    return parts


def _build_sources_footer(result: AgentRunResult[str]) -> str:
    seen: dict[str, int] = {}
    sources: list[tuple[str, str]] = []
    for msg in result.all_messages():
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if not isinstance(part, BuiltinToolReturnPart):
                continue
            if part.tool_name not in ("web_search", "web_fetch"):
                continue
            if not isinstance(part.content, list):
                continue
            for chunk in part.content:
                if not isinstance(chunk, dict):
                    continue
                uri = chunk.get("uri") or chunk.get("retrieved_url") or ""
                title = chunk.get("title", uri)
                if uri and uri not in seen:
                    seen[uri] = len(sources) + 1
                    sources.append((title, uri))
    if not sources:
        return ""
    lines = ["**Sources**"]
    for i, (title, url) in enumerate(sources, 1):
        lines.append(f"{i}. [{title}]({url})")
    return "\n".join(lines)
