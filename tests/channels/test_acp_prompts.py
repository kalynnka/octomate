from __future__ import annotations

import pytest
from acp import RequestError, schema
from pydantic import JsonValue
from uuid_utils.compat import uuid7

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.tentacles.acp.adapter import prompt_segments


def test_prompt_preserves_order_and_text_through_octomate_message_segments() -> None:
    request = schema.PromptRequest.model_validate(
        {
            "sessionId": str(uuid7()),
            "prompt": [
                {"type": "text", "text": "  Review 你好🐙\n"},
                {
                    "type": "resource_link",
                    "uri": "file:///workspace/main.py",
                    "name": "main.py",
                    "title": "Entry point",
                },
                {"type": "text", "text": "\nKeep the formatting.  "},
            ],
        }
    )
    original = request.model_dump_json()
    event = MessageEvent(segments=prompt_segments(request))
    restored = MessageEvent.model_validate_json(event.model_dump_json())

    assert all(isinstance(segment, TextSegment) for segment in restored.segments)
    assert restored.text_parts() == [
        "  Review 你好🐙\n",
        "Entry point (file:///workspace/main.py)",
        "\nKeep the formatting.  ",
    ]
    assert request.model_dump_json() == original


@pytest.mark.parametrize(
    "uri", ["file:///missing/file.txt", "https://example.invalid/doc"]
)
@pytest.mark.parametrize("name", ["Document", ""])
def test_resource_links_remain_references_without_accessing_the_resource(
    uri: str, name: str
) -> None:
    request = schema.PromptRequest(
        session_id=str(uuid7()),
        prompt=[schema.ResourceContentBlock(type="resource_link", uri=uri, name=name)],
    )

    segments = prompt_segments(request)

    assert [str(segment) for segment in segments] == [
        f"{name} ({uri})" if name else uri
    ]


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "mimeType": "image/png", "data": "aW1hZ2U="},
        {"type": "audio", "mimeType": "audio/wav", "data": "YXVkaW8="},
        {
            "type": "resource",
            "resource": {"uri": "file:///note.txt", "text": "Embedded context"},
        },
        {
            "type": "resource",
            "resource": {"uri": "file:///data.bin", "blob": "YmluYXJ5"},
        },
    ],
)
def test_unsupported_content_rejects_the_prompt_instead_of_returning_partial_text(
    block: dict[str, JsonValue],
) -> None:
    request = schema.PromptRequest.model_validate(
        {
            "sessionId": str(uuid7()),
            "prompt": [{"type": "text", "text": "Describe this"}, block],
        }
    )

    with pytest.raises(RequestError) as raised:
        prompt_segments(request)

    assert raised.value.code == -32602
    assert raised.value.data == {
        "reason": f"Unsupported prompt content: {block['type']}"
    }


@pytest.mark.parametrize("texts", [[], [""], [" ", "\n\t"]])
def test_empty_prompt_is_rejected(texts: list[str]) -> None:
    request = schema.PromptRequest(
        session_id=str(uuid7()),
        prompt=[schema.TextContentBlock(type="text", text=text) for text in texts],
    )

    with pytest.raises(RequestError) as raised:
        prompt_segments(request)

    assert raised.value.code == -32602
    assert raised.value.data == {"reason": "Prompt carried no text"}
