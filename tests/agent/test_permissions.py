"""The approval vocabulary is declared twice, and these keep the two together.

Each tentacle class owns what it accepts (`permission_modes`), because that is a fact
about the provider rather than about a row. The schema layer needs the same vocabulary
to refuse a posture on write, and cannot import a tentacle — so `PERMISSION_MODES`
carries it there, keyed by the id `main.py` registers each tentacle under. Order counts
in both: a picker steps through the vocabulary, so the two must agree on the sequence
and not merely on the members.

`PERMISSION_MODES` is keyed slightly wider than the registry: the two native ids are in
it as well, carrying their provider's vocabulary, because a session Octomate only tails
is still in one of those postures and its transcript says which. No tentacle class backs
either, which is exactly why the console cannot switch one.

Two declarations of one fact drift silently. These fail instead.
"""

from __future__ import annotations

import pytest

from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.inkling import InklingTentacle
from octomate.types.permissions import PERMISSION_MODES, check_mode
from octomate.types.threads import (
    CLAUDE_NATIVE_ID,
    CODEX_NATIVE_ID,
    NATIVE_TENTACLE_IDS,
)

# The ids `main.py` connects each tentacle under, which are what a conversation row
# stores and what `PERMISSION_MODES` is keyed by.
REGISTERED: dict[str, type[AgentTentacle]] = {
    "inkling": InklingTentacle,
    "claude": ClaudeCodeTentacle,
    "codex": CodexTentacle,
    "deepseek": DeepseekTentacle,
}


@pytest.mark.parametrize(("agent_tentacle_id", "tentacle"), REGISTERED.items())
def test_the_schema_map_says_what_the_tentacle_accepts(
    agent_tentacle_id: str, tentacle: type[AgentTentacle]
) -> None:
    assert PERMISSION_MODES[agent_tentacle_id] == tentacle.permission_modes


def test_every_registered_agent_is_in_the_schema_map() -> None:
    assert set(REGISTERED) <= set(PERMISSION_MODES)


def test_a_tailed_runtime_keeps_its_providers_vocabulary() -> None:
    """Observed rather than driven, but observed *in* a posture — so the id carries the
    same scale the tentacle it mirrors does, and a transcript's mode stores as-is."""
    assert PERMISSION_MODES[CLAUDE_NATIVE_ID] == ClaudeCodeTentacle.permission_modes
    assert PERMISSION_MODES[CODEX_NATIVE_ID] == CodexTentacle.permission_modes
    check_mode(CLAUDE_NATIVE_ID, "bypassPermissions")
    check_mode(CODEX_NATIVE_ID, "auto_review")


def test_the_map_holds_nothing_but_agents_and_the_runtimes_they_mirror() -> None:
    # Every other id — a channel's, a typo — is refused a posture rather than given one
    # nothing reads.
    assert set(PERMISSION_MODES) == set(REGISTERED) | NATIVE_TENTACLE_IDS


@pytest.mark.parametrize(("agent_tentacle_id", "tentacle"), REGISTERED.items())
def test_check_mode_accepts_exactly_the_tentacles_own(
    agent_tentacle_id: str, tentacle: type[AgentTentacle]
) -> None:
    for mode in tentacle.permission_modes:
        check_mode(agent_tentacle_id, mode)  # pyright: ignore[reportArgumentType]

    for other, others_modes in PERMISSION_MODES.items():
        if other == agent_tentacle_id:
            continue
        for mode in set(others_modes) - set(tentacle.permission_modes):
            with pytest.raises(ValueError, match="is not one of"):
                check_mode(agent_tentacle_id, mode)  # pyright: ignore[reportArgumentType]


def test_an_agent_with_no_vocabulary_is_refused_a_posture() -> None:
    # A channel is not an agent and never runs anything, so nothing would read one.
    with pytest.raises(ValueError, match="has no permission modes"):
        check_mode("slack", "default")
