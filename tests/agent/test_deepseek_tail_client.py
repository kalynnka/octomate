"""The dsh tail client's reading half: gateway paging back to the cursor, and
the one safety rule it applies — never ship a trailing turn whose interrupted
close may be the gateway reader's in-memory synthesis for a turn some other
process is still writing."""

from __future__ import annotations

import pytest
from octomate_cli.deepseek import tail as deepseek_tail
from octomate_cli.deepseek.tail import new_entries, seq_of, session_origin, shippable

DSH_URL = "http://127.0.0.1:3080"
SESSION_ID = "session-native-0001"


def event(seq: int, kind: str, data: object = None) -> dict[str, object]:
    return {"type": kind, "seq": seq, "time": 1.0 + seq, "data": data}


def entry(seq: int, kind: str, data: object = None) -> dict[str, object]:
    return {"event": event(seq, kind, data)}


def turn(base: int, *, reason: str = "completed") -> list[dict[str, object]]:
    # The log's real per-turn order: the turn opens first, the prompt splices
    # in after it.
    return [
        entry(base, "turn/start", {"turn": 1}),
        entry(base + 1, "user/message", {"content": [{"type": "text", "text": "ask"}]}),
        entry(
            base + 2, "assistant/chunk", {"chunk": {"type": "text-delta", "text": "a"}}
        ),
        entry(base + 3, "turn/end", {"turn": 1, "reason": {"kind": reason}}),
    ]


class PagingGateway:
    """`session.history` served in fixed windows counted back from the tail,
    `beforeSeq` exclusive — the paging shape the real gateway answers with."""

    def __init__(self, entries: list[dict[str, object]], page_size: int) -> None:
        self.entries = entries
        self.page_size = page_size
        self.calls: list[dict[str, object]] = []

    def __call__(
        self, dsh_url: str, method: str, payload: dict[str, object]
    ) -> object | None:
        self.calls.append({"method": method, **payload})
        if method != "session.history":
            return None
        before = payload.get("beforeSeq")
        assert before is None or isinstance(before, int)
        window = [
            item
            for item in self.entries
            if before is None or ((seq := seq_of(item)) is not None and seq < before)
        ]
        page = window[-self.page_size :]
        return {"events": page, "hasMore": len(window) > len(page)}


def test_new_entries_pages_back_to_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = PagingGateway([*turn(0), *turn(4)], page_size=3)
    monkeypatch.setattr(deepseek_tail, "rpc", gateway)

    entries = new_entries(DSH_URL, SESSION_ID, 2)

    assert [seq_of(item) for item in entries] == [2, 3, 4, 5, 6, 7]
    # Two windows of three walked the tail back to the cursor, no further.
    assert len(gateway.calls) == 2


def test_new_entries_from_zero_takes_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = PagingGateway(turn(0), page_size=100)
    monkeypatch.setattr(deepseek_tail, "rpc", gateway)

    entries = new_entries(DSH_URL, SESSION_ID, 0)

    assert [seq_of(item) for item in entries] == [0, 1, 2, 3]
    assert len(gateway.calls) == 1


def test_a_gateway_failure_ships_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepseek_tail, "rpc", lambda *args: None)
    assert new_entries(DSH_URL, SESSION_ID, 0) == []


def test_a_completed_tail_ships_whole() -> None:
    entries = turn(0)
    assert shippable(entries) == entries


def test_an_interrupted_tail_withholds_the_trailing_turn() -> None:
    """The interrupted close may be the reader's synthesis for a turn another
    process is still writing; shipping it would advance the cursor past seqs
    whose truth is not yet written."""
    entries = [*turn(0), *turn(4, reason="interrupted")]

    shipped = shippable(entries)

    # The first turn ships whole; the trailing turn — prompt included — waits.
    assert [seq_of(item) for item in shipped] == [0, 1, 2, 3]


def test_an_interrupted_turn_with_a_successor_ships() -> None:
    entries = [
        *turn(0, reason="interrupted"),
        entry(4, "session/title", {"title": "t"}),
    ]
    assert shippable(entries) == entries


def test_session_origin_reads_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    def listing(dsh_url: str, method: str, payload: dict[str, object]) -> object:
        assert method == "session.list"
        return {
            "items": [
                {"sessionId": "session-parent"},
                {"sessionId": SESSION_ID, "origin": "subagent"},
            ]
        }

    monkeypatch.setattr(deepseek_tail, "rpc", listing)
    assert session_origin(DSH_URL, SESSION_ID) == "subagent"
    assert session_origin(DSH_URL, "session-parent") is None
