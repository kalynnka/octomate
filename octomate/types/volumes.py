"""Logical volume behavior and application usage."""

from __future__ import annotations

from typing import Literal

VolumeKind = Literal["directory", "git"]
VolumeUsage = Literal["tree", "memory", "cache"]
