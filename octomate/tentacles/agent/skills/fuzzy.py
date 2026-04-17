from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz


@dataclass
class MatchResult:
    start: int
    end: int
    strategy: str


def find_match(
    text: str,
    old: str,
    *,
    threshold: int = 92,
) -> MatchResult:
    """Find ``old`` inside ``text`` using a 4-strategy cascade.

    Raises ``ValueError`` if no match or if the match is ambiguous (multiple
    candidates at equal score).
    """
    for strategy, finder in [
        ("exact", _exact),
        ("whitespace_normalized", _whitespace_normalized),
        ("line_trimmed", _line_trimmed),
        ("token_set_ratio", lambda t, o: _token_set_ratio(t, o, threshold)),
    ]:
        result = finder(text, old)
        if result is not None:
            if isinstance(result, list):
                if len(result) == 1:
                    m = result[0]
                    return MatchResult(m[0], m[1], strategy)
                if len(result) > 1:
                    excerpts = " | ".join(
                        repr(text[s:e][:60]) for s, e in result[:3]
                    )
                    raise ValueError(
                        f"patch_skill: ambiguous match ({len(result)} candidates) "
                        f"using strategy '{strategy}'. Provide more context. "
                        f"Candidates: {excerpts}"
                    )
            else:
                return MatchResult(result[0], result[1], strategy)

    raise ValueError(
        f"patch_skill: could not find old_string in skill content.\n"
        f"First 200 chars of file:\n{text[:200]}"
    )


def apply_patch(text: str, match: MatchResult, new: str) -> str:
    return text[: match.start] + new + text[match.end :]


# --- Strategy 1: exact --------------------------------------------------

def _exact(text: str, old: str) -> tuple[int, int] | None:
    idx = text.find(old)
    if idx == -1:
        return None
    # Verify uniqueness
    second = text.find(old, idx + 1)
    if second != -1:
        return [(idx, idx + len(old)), (second, second + len(old))]  # type: ignore[return-value]
    return (idx, idx + len(old))


# --- Strategy 2: whitespace-normalized ----------------------------------

def _whitespace_normalized(text: str, old: str) -> tuple[int, int] | list[tuple[int, int]] | None:
    norm_old = re.sub(r"\s+", " ", old).strip()
    if not norm_old:
        return None

    # Build a char-index table: norm_pos → original_pos for each non-space run
    norm_text, index_table = _normalize_with_table(text)

    idx = norm_text.find(norm_old)
    if idx == -1:
        return None

    matches = []
    start = idx
    while start != -1:
        end = start + len(norm_old)
        orig_start = index_table[start]
        # Find original end: walk back from next char's origin if possible
        if end < len(index_table):
            # The norm match ends at end; orig end is the character in text
            # corresponding to the last norm char + its original length
            orig_end = _find_orig_end(text, norm_text, index_table, start, end)
        else:
            orig_end = len(text)
        matches.append((orig_start, orig_end))
        start = norm_text.find(norm_old, end)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches
    return None


def _normalize_with_table(text: str) -> tuple[str, list[int]]:
    """Return (normalized_text, index_table) where index_table[i] = orig pos of norm[i]."""
    norm_chars: list[str] = []
    table: list[int] = []
    i = 0
    prev_space = True
    while i < len(text):
        c = text[i]
        if c in " \t\r\n":
            if not prev_space:
                norm_chars.append(" ")
                table.append(i)
                prev_space = True
        else:
            norm_chars.append(c)
            table.append(i)
            prev_space = False
        i += 1
    return "".join(norm_chars).strip(), table


def _find_orig_end(
    text: str, norm_text: str, table: list[int], norm_start: int, norm_end: int
) -> int:
    if norm_end >= len(table):
        return len(text)
    # The character in norm_text at norm_end is the first char NOT in the match.
    # Its original position is the boundary.
    next_orig = table[norm_end]
    # We need to include trailing whitespace that was collapsed into the last space
    # in the normalized string.  Walk backwards through any whitespace.
    orig_last_char = table[norm_end - 1]
    # Include all whitespace after orig_last_char up to next_orig
    end = orig_last_char + 1
    while end < next_orig and text[end] in " \t\r\n":
        end += 1
    return end


# --- Strategy 3: line-trimmed -------------------------------------------

def _line_trimmed(text: str, old: str) -> tuple[int, int] | list[tuple[int, int]] | None:
    old_lines = [l.strip() for l in old.splitlines()]
    if not any(old_lines):
        return None
    text_lines_raw = text.splitlines(keepends=True)
    text_lines_stripped = [l.strip() for l in text_lines_raw]

    n = len(old_lines)
    matches: list[tuple[int, int]] = []

    for i in range(len(text_lines_stripped) - n + 1):
        if text_lines_stripped[i : i + n] == old_lines:
            # Compute character offsets
            start = sum(len(text_lines_raw[j]) for j in range(i))
            end = start + sum(len(text_lines_raw[j]) for j in range(i, i + n))
            matches.append((start, end))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches
    return None


# --- Strategy 4: rapidfuzz token_set_ratio over sliding windows ----------

def _token_set_ratio(
    text: str, old: str, threshold: int
) -> tuple[int, int] | list[tuple[int, int]] | None:
    old_len = len(old)
    if old_len == 0 or old_len > len(text):
        return None

    window_min = max(1, int(old_len * 0.8))
    window_max = min(len(text), int(old_len * 1.2))

    best_score = 0.0
    best_matches: list[tuple[int, int]] = []

    step = max(1, old_len // 4)
    for start in range(0, len(text) - window_min + 1, step):
        for wlen in range(window_min, min(window_max, len(text) - start) + 1, step):
            candidate = text[start : start + wlen]
            score = fuzz.token_set_ratio(old, candidate)
            if score >= threshold:
                if score > best_score + 5:
                    best_score = score
                    best_matches = [(start, start + wlen)]
                elif abs(score - best_score) <= 5:
                    best_matches.append((start, start + wlen))

    if not best_matches:
        return None
    if len(best_matches) == 1:
        return best_matches[0]
    return best_matches
