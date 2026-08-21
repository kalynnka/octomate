from __future__ import annotations

from pydantic import BaseModel, Field


class WorkspacesConfig(BaseModel):
    """When a thread's workspace is reclaimed."""

    idle_window: float = Field(
        default=24 * 60 * 60.0,
        gt=0.0,
        description=(
            "Seconds a workspace goes unused before the sweep may reclaim it, "
            "measured from the last turn that ran in it. Generous by default: "
            "every turn leaves its work in the mirror, so reclaiming early costs "
            "a resume and never the work — but a resume is a fork and a checkout, "
            "and a fork nobody has touched for a day is what the disk is for. "
            "Shorten it on a host tight for space, not to tidy up."
        ),
    )
    sweep_interval: float = Field(
        default=60 * 60.0,
        gt=0.0,
        description=(
            "Seconds between sweeps. Every idle workspace is snapshotted again to "
            "prove the mirror already has it, so this is work proportional to how "
            "many are lying around; there is nothing to gain by looking often."
        ),
    )
