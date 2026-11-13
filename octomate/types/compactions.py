"""Reported causes of completed context transformations."""

from typing import Literal, NamedTuple

from pydantic import UUID7

type CompactionTrigger = Literal["manual", "auto", "unknown"]
type CompactionStage = Literal["before", "after"]


class CompactionRewrite(NamedTuple):
    """Ordered durable message identities before and after one transformation."""

    compaction_id: UUID7
    before: list[UUID7]
    after: list[UUID7]


class CompactionSessionKey(NamedTuple):
    """Serialize observations of one native session across conversation owners."""

    agent_tentacle_id: str
    native_session_id: str
