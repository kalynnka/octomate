"""Reported causes of completed context transformations."""

from typing import Literal, NamedTuple

type CompactionTrigger = Literal["manual", "auto", "unknown"]
type CompactionStage = Literal["before", "after"]


class CompactionSessionKey(NamedTuple):
    """Serialize observations of one native session across conversation owners."""

    agent_tentacle_id: str
    native_session_id: str
