"""The approval vocabulary is declared twice, and these keep the two together.

Each tentacle class owns what it accepts (`permission_modes`), because that is a fact
about the provider rather than about a row. The schema layer needs the same vocabulary
to refuse a posture on write, and cannot import a tentacle — so `PERMISSION_MODES`
carries it there, keyed by the id `main.py` registers each tentacle under. Order counts
in both: a picker steps through the vocabulary, so the two must agree on the sequence
and not merely on the members.

Two declarations of one fact drift silently. These fail instead.
"""

from __future__ import annotations

import pytest

from octomate.tentacles.agents.base import AgentTentacle
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.inkling import InklingTentacle
from octomate.types.permissions import PERMISSION_MODES, check_mode

# The ids `main.py` connects each tentacle under, which are what a conversation row
# stores and what `PERMISSION_MODES` is keyed by.
REGISTERED: dict[str, type[AgentTentacle]] = {
    "inkling": InklingTentacle,
    "claude": ClaudeCodeTentacle,
    "codex": CodexTentacle,
}


@pytest.mark.parametrize(("agent_tentacle_id", "tentacle"), REGISTERED.items())
def test_the_schema_map_says_what_the_tentacle_accepts(
    agent_tentacle_id: str, tentacle: type[AgentTentacle]
) -> None:
    assert PERMISSION_MODES[agent_tentacle_id] == tentacle.permission_modes


def test_every_registered_agent_is_in_the_schema_map() -> None:
    assert set(PERMISSION_MODES) == set(REGISTERED)


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
    # A native session is observed rather than driven, so nothing would read one.
    with pytest.raises(ValueError, match="has no permission modes"):
        check_mode("claude-native", "default")
