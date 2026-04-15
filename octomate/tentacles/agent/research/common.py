from __future__ import annotations

import re
from typing import Any

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import FileSegment, ImageSegment

URL_RE = re.compile(r"https?://[^\s<>\"'\\)\]}]+")


def build_input(contents: list[MessageEvent]) -> list[dict[str, Any]]:
    """Convert MessageEvent contents into Interactions API input parts.

    Text is concatenated into a single text part. Image/file segments are
    appended as typed parts with URIs. URLs found in text are left inline —
    the url_context tool will handle fetching them.
    """
    texts: list[str] = []
    media_parts: list[dict[str, Any]] = []

    for event in contents:
        texts.extend(event.text_parts())
        for seg in event.segments:
            if isinstance(seg, ImageSegment):
                if seg.data.url:
                    media_parts.append({"type": "image", "uri": seg.data.url})
            elif isinstance(seg, FileSegment):
                if seg.data.url:
                    media_parts.append({"type": "document", "uri": seg.data.url})

    parts: list[dict[str, Any]] = []
    combined = "\n".join(texts).strip()
    if combined:
        parts.append({"type": "text", "text": combined})
    parts.extend(media_parts)
    return parts or [{"type": "text", "text": ""}]


def extract_urls(contents: list[MessageEvent]) -> list[str]:
    """Extract all HTTP(S) URLs from message text segments."""
    urls: list[str] = []
    for event in contents:
        for text in event.text_parts():
            urls.extend(URL_RE.findall(text))
    return urls


def format_citations(text: str, annotations: list[dict[str, Any]]) -> str:
    """Insert footnote markers and append a Sources section.

    Processes url_citation annotations from the Interactions API, inserting
    [n] markers at citation positions and building a numbered source list.
    """
    if not annotations:
        return text

    citations = [a for a in annotations if a.get("type") == "url_citation"]
    if not citations:
        return text

    seen: dict[str, int] = {}
    sources: list[tuple[str, str]] = []
    for cite in citations:
        url = cite.get("url", "")
        if url and url not in seen:
            seen[url] = len(sources) + 1
            sources.append((cite.get("title", url), url))

    sorted_cites = sorted(citations, key=lambda c: c.get("end_index", 0), reverse=True)
    result = text
    for cite in sorted_cites:
        url = cite.get("url", "")
        idx = seen.get(url)
        if idx is None:
            continue
        end = cite.get("end_index")
        if end is not None and 0 <= end <= len(result):
            marker = f" [{idx}]"
            result = result[:end] + marker + result[end:]

    if sources:
        lines = ["\n\n---\n**Sources**\n"]
        for i, (title, url) in enumerate(sources, 1):
            lines.append(f"{i}. [{title}]({url})")
        result += "\n".join(lines)

    return result


def build_sources_footer(annotations: list[dict[str, Any]]) -> str:
    """Build a compact sources footer from url_citation annotations."""
    seen: dict[str, int] = {}
    sources: list[tuple[str, str]] = []
    for a in annotations:
        if a.get("type") != "url_citation":
            continue
        url = a.get("url", "")
        if url and url not in seen:
            seen[url] = len(sources) + 1
            sources.append((a.get("title", url), url))
    if not sources:
        return ""
    lines = ["**Sources**"]
    for i, (title, url) in enumerate(sources, 1):
        lines.append(f"{i}. [{title}]({url})")
    return "\n".join(lines)
