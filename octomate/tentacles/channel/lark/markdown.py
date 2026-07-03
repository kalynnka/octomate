from __future__ import annotations

import re

TABLE_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")
CARD_RENDER_FALLBACK_HINT = (
    "I couldn't render this as a Lark card, so here's the raw message:"
)


def is_table_separator(line: str) -> bool:
    cells = [cell.strip().replace(" ", "") for cell in line.strip().strip("|").split("|")]
    return len(cells) >= 2 and all(
        TABLE_SEPARATOR_CELL_RE.fullmatch(cell) is not None for cell in cells
    )


def is_table_row(line: str) -> bool:
    return "|" in line and bool(line.strip())


def preserve_markdown_tables_as_text(content: str) -> str:
    """Render Markdown tables as text so Lark cards do not create table elements."""
    # TODO: Render bounded Markdown tables as native Lark card tables, while
    # keeping this text fallback for oversized or unsupported tables.
    lines = content.splitlines()
    if len(lines) < 2:
        return content

    rendered: list[str] = []
    in_fence = False
    changed = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            rendered.append(line)
            index += 1
            continue
        if (
            not in_fence
            and index + 1 < len(lines)
            and is_table_row(line)
            and is_table_separator(lines[index + 1])
        ):
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and is_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            rendered.append("```text")
            rendered.extend(table_lines)
            rendered.append("```")
            changed = True
            continue
        rendered.append(line)
        index += 1

    return "\n".join(rendered) if changed else content


def card_render_fallback_text(raw_message: str) -> str:
    return f"{CARD_RENDER_FALLBACK_HINT}\n\n{raw_message}"
