from __future__ import annotations

import re
from mimetypes import guess_extension, guess_type

# TODO: Maybe pandoc in future?


def guess_image_ext(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip()
    ext = guess_extension(ct) if ct.startswith("image/") else None
    if not ext:
        ext = guess_extension(guess_type(url)[0] or "") or ".png"
    return ext


_MD_PATTERNS = [
    r"\*\*.+?\*\*",  # **bold**
    r"__.+?__",  # __bold__
    r"(?<!\*)\*(?!\*).+?(?<!\*)\*(?!\*)",  # *italic* (not **)
    r"~~.+?~~",  # ~~strikethrough~~
    r"`.+?`",  # `inline code`
    r"^```",  # code fence
    r"^\#{1,6}\s",  # # headers
    r"\[.+?\]\(.+?\)",  # [links](url)
    r"^>\s",  # > blockquote
    r"^\|.+\|",  # | table |
]
_MD_RE = re.compile("|".join(_MD_PATTERNS), re.MULTILINE)


def has_markdown(text: str) -> bool:
    return bool(_MD_RE.search(text))


def strip_markdown(text: str) -> str:
    # Code fences: keep content, remove ``` lines
    text = re.sub(r"^```\w*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```$", "", text, flags=re.MULTILINE)
    # Inline code
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Bold/italic combos: ***text*** or ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text)
    # Bold: **text** or __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text)
    # Italic: *text* or _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text)
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Links: [text](url) → text (url)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 (\2)", text)
    # Headers: remove leading #
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Blockquotes: remove leading >
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    return text
