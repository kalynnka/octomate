"""Reusable Lark interactive-card element builders.

Lark cards are plain JSON objects. These builders keep the element shapes in one
place so the approval/question/output feelers don't each hand-roll the same dicts.
They return `JsonObject`; callers assemble them into a card envelope (legacy
``{header, elements}`` via `simple_card`, or a schema-2.0 streaming card).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

from octomate.types.json import JsonObject


def header(title: str, *, template: str | None = None) -> JsonObject:
    head: JsonObject = {"title": {"tag": "plain_text", "content": title}}
    if template is not None:
        head["template"] = template
    return head


def markdown(content: str, *, element_id: str | None = None) -> JsonObject:
    element: JsonObject = {
        "tag": "markdown",
        "content": content,
    }
    if element_id is not None:
        element["element_id"] = element_id
    return element


def divider() -> JsonObject:
    return {"tag": "hr"}


def button(
    text: str,
    *,
    button_type: str = "default",
    value: JsonObject | None = None,
    action_type: str | None = None,
    url: str | None = None,
    name: str | None = None,
) -> JsonObject:
    element: JsonObject = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
    }
    if action_type is not None:
        element["action_type"] = action_type
    # A `link` button's target belongs here, on the element itself — Lark never
    # reads `value` for a jump, and rejects the whole card if this is missing.
    if url is not None:
        element["url"] = url
    if name is not None:
        element["name"] = name
    if value is not None:
        element["value"] = value
    return element


def action(buttons: Sequence[JsonValue]) -> JsonObject:
    return {"tag": "action", "actions": list(buttons)}


def collapsible_panel(
    title: str,
    elements: Sequence[JsonValue],
    *,
    expanded: bool = False,
) -> JsonObject:
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title},
            # A right-aligned chevron that flips when open — the affordance that
            # the panel is clickable to expand/collapse.
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": list(elements),
    }


def card_v2(
    elements: Sequence[JsonValue],
    *,
    header: JsonObject | None = None,  # noqa: A002 - card field name
) -> JsonObject:
    """A schema-2.0 card envelope — required for 2.0-only components like
    `collapsible_panel` (the legacy ``{header, elements}`` form rejects them)."""
    card: JsonObject = {"schema": "2.0"}
    if header is not None:
        card["header"] = header
    card["body"] = {"elements": list(elements)}
    return card


def simple_card(
    elements: Sequence[JsonValue],
    *,
    header: JsonObject | None = None,  # noqa: A002 - card field name
) -> JsonObject:
    card: JsonObject = {}
    if header is not None:
        card["header"] = header
    card["elements"] = list(elements)
    return card
