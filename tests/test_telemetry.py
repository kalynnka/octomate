from __future__ import annotations

import json

from pydantic_ai.messages import TextContent

from octomate.telemetry import agent_input_message_attributes


def test_agent_input_messages_render_every_user_prompt_segment() -> None:
    attributes = agent_input_message_attributes(
        [
            TextContent(content="@Octomate"),
            "summon claude+fable in a new thread.",
        ]
    )

    assert json.loads(attributes["gen_ai.input.messages"]) == [
        {
            "role": "user",
            "parts": [
                {"type": "text", "content": "@Octomate"},
                {
                    "type": "text",
                    "content": "summon claude+fable in a new thread.",
                },
            ],
        }
    ]
    assert json.loads(attributes["logfire.json_schema"]) == {
        "type": "object",
        "properties": {"gen_ai.input.messages": {"type": "array"}},
    }
