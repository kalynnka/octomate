"""Translate this harness's injected context into optional message annotations."""

from __future__ import annotations

import re

from pydantic_ai.messages import TextContent

from octomate.schemas.annotations import MessageAnnotations, MessageContext


def prompt_content(text: str) -> str | TextContent:
    if not text.startswith(
        ("<ide_opened_file>", "<ide_selection>", "<browser_instruction>")
    ):
        return text
    result = MessageAnnotations(raw=text, source="claude")
    body = text
    titles = {
        "ide_opened_file": "Opened file",
        "ide_selection": "Selection",
        "browser_instruction": "Browser instructions",
    }
    while match := re.match(
        r"<(ide_opened_file|ide_selection|browser_instruction)>([\s\S]*?)</\1>(?:\r?\n)?",
        body,
    ):
        tag, content = match.groups()
        opened = re.match(r"The user opened the file (.+?) in the IDE\.", content)
        selection = re.match(
            r"The user selected the lines (\d+) to (\d+) from (.+?):\r?\n", content
        )
        if (tag == "ide_opened_file" and not opened) or (
            tag == "ide_selection" and not selection
        ):
            break
        if tag == "browser_instruction" and not content.startswith(
            ("# Claude in Chrome browser automation", "[Browser disconnected:")
        ):
            break
        result.contexts.append(MessageContext(title=titles[tag], text=content))
        body = body[match.end() :]
        if tag == "ide_opened_file" and opened:
            result.reference(opened[1], "Opened file")
        if tag == "ide_selection" and selection:
            result.reference(
                selection[3], "Selection", int(selection[1]), int(selection[2])
            )
    return (
        TextContent(body, metadata={"annotations": result.model_dump(mode="json")})
        if result.contexts
        else text
    )
