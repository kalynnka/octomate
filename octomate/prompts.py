"""System markings inside a prompt.

A prompt carries more than the message it answers: the chat behind it, a brief
another agent wrote, a notice a runtime raised while it was working. Each of those is
wrapped in a tag, the way an editor marks what it puts in front of a model, so two
readers can tell them apart — the model, which must not answer the context as if it
were the ask, and whatever renders the prompt back to a person, which should show
what was said and not the scaffolding around it.

The message being answered is never tagged. It is the body; everything the system
added stands around it, and `untagged` is what takes the scaffolding away again.
"""

from __future__ import annotations

import re
from typing import Literal, TypeAlias, get_args

# Every marking the tree emits. `untagged` strips these and nothing else, so a
# person who writes `<summary>` in a message keeps it.
PromptTag: TypeAlias = Literal["chat_recap", "instructions"]


def tagged(tag: PromptTag, body: str) -> str:
    """`body` under its marking, or nothing at all when there is no body — an empty
    marking is noise in the prompt and a stray pair of tags in a render."""
    if not body:
        return ""
    return f"<{tag}>\n{body}\n</{tag}>"


MARKINGS = re.compile(
    rf"<(?P<tag>{'|'.join(get_args(PromptTag))})>\n.*?\n</(?P=tag)>\n*",
    re.DOTALL,
)


def untagged(text: str) -> str:
    """`text` with every marking taken out: what a person actually wrote.

    For rendering a prompt back to someone — a thread view showing the turn that
    ran — where the chat behind it is already on screen above.
    """
    return MARKINGS.sub("", text).strip()
