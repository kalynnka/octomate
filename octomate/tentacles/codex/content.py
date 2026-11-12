"""Translate this harness's injected context into optional message annotations."""

from __future__ import annotations

import re

from pydantic_ai.messages import TextContent, TextPart

from octomate.schemas.annotations import MessageAnnotations, MessageContext


def prompt_content(text: str, *, ide_context: str | None = None) -> str | TextContent:
    if ide_context is not None:
        context = ide_context.removeprefix("<external_ide_context>").removesuffix(
            "</external_ide_context>"
        )
        if not context.startswith("# Context from my IDE setup:\n"):
            return text
        body = text
    else:
        if not text.startswith("# Context from my IDE setup:\n"):
            return text
        context, boundary, body = text.partition("\n## My request:\n")
        if not boundary:
            return text
        body = body.rstrip("\n")
    result = MessageAnnotations(raw=text, source="codex")
    result.contexts.append(MessageContext(title="IDE context", text=context))
    tabs = False
    active_file = ""
    for line in context.splitlines():
        if line.startswith("## Active file: "):
            active_file = line.removeprefix("## Active file: ")
            result.reference(active_file, "Active file")
        if line == "## Active selection of the file:" and active_file:
            result.reference(active_file, "Selection")
        if line.startswith("## "):
            tabs = line == "## Open tabs:"
        if tabs and line.startswith("- ") and ": " in line:
            result.reference(line.partition(": ")[2], "Open tab")
    return TextContent(body, metadata={"annotations": result.model_dump(mode="json")})


def answer_part(text: str, *, partial: bool = False) -> TextPart:
    part = TextPart(text)
    marker = "<oai-mem-citation>"
    if "<" not in text:
        return part
    start = text.rfind(marker)
    if start < 0 and partial:
        tail = text.rsplit("\n", 1)[-1]
        if tail and marker.startswith(tail):
            start = len(text) - len(tail)
    if start < 0 or (start and text[start - 1] != "\n"):
        return part
    # A citation example inside a Markdown fence is prose, not an annotation.
    fence = ""
    for line in text[:start].splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match and not fence:
            fence = match[1]
        elif (
            match
            and match[1][0] == fence[:1]
            and len(match[1]) >= len(fence)
            and not match[2].strip()
        ):
            fence = ""
    if fence:
        return part
    envelope = text[start:]
    result = MessageAnnotations(raw=text, source="codex")
    match = re.fullmatch(
        r"<oai-mem-citation>\s*<citation_entries>\s*([\s\S]*?)\s*</citation_entries>\s*<rollout_ids>\s*([\s\S]*?)\s*</rollout_ids>\s*</oai-mem-citation>\s*",
        envelope,
    )
    if match is None:
        if partial and "</oai-mem-citation>" not in envelope:
            part.content = text[:start].rstrip()
        return part
    for entry in match[1].splitlines():
        citation = re.fullmatch(
            r"(.+?):(\d+)(?:-(\d+))?\|note=\[(.*?)\]", entry.strip()
        )
        if citation is None:
            return part
        path, first, last, note = citation.groups()
        start_line, end_line = int(first), int(last or first)
        if start_line < 1 or end_line < start_line:
            return part
        result.reference(path, "Memory source", start_line, end_line, note)
    ids = match[2].split()
    if not result.references or any(
        re.fullmatch(r"[\da-fA-F]{8}(?:-[\da-fA-F]{4}){3}-[\da-fA-F]{12}", value)
        is None
        for value in ids
    ):
        return part
    result.contexts.append(
        MessageContext(
            title="Memory sources",
            text="\n".join(
                f"{ref.path}:{ref.start_line} — {ref.note}" for ref in result.references
            ),
            rollout_ids=ids,
        )
    )
    part.content = text[:start].rstrip()
    part.provider_name = "openai_codex"
    part.provider_details = {"annotations": result.model_dump(mode="json")}
    return part
