"""System markings inside a prompt: what a model is handed, and what a render
takes back out."""

from __future__ import annotations

from octomate.prompts import tagged, untagged


def test_a_marking_wraps_its_body() -> None:
    assert tagged("chat_recap", "alice: hello") == (
        "<chat_recap>\nalice: hello\n</chat_recap>"
    )


def test_an_empty_body_is_not_marked() -> None:
    """A chat room with nothing behind the ask would otherwise put an empty pair of
    tags in the prompt, and a stray one in every render of it."""
    assert tagged("chat_recap", "") == ""


def test_a_render_keeps_only_what_a_person_wrote() -> None:
    recap = tagged("chat_recap", "alice: hello\n\nbot: handled")

    assert untagged(f"{recap}\n\nalice: and now?") == "alice: and now?"


def test_a_marking_a_person_typed_survives() -> None:
    """Only the markings this tree emits are stripped. Somebody writing about XML,
    or pasting a diff, keeps every angle bracket they typed."""
    prompt = "does <summary>\nlike this\n</summary> work in the template?"

    assert untagged(prompt) == prompt
