"""Where a run happens, and everything that has to be true before it can.

Two layers, and the upper one only knows about the one below it. `mirrors` keeps
one pristine checkout per project, made from its upstream and never written to by
a run. `base` forks a mirror into the workspace a thread runs in, and decides what
becomes of it when the run ends.

They are together because they are one story told in order — a thread asks for a
workspace, which needs a mirror, which needs to be current before it is worth
forking — and apart from each other because the seam is real: nothing in `mirrors`
knows what a thread is.
"""

from __future__ import annotations

from octomate.managers.workspaces.base import WorkspaceManager
from octomate.managers.workspaces.mirrors import MirrorManager

__all__ = [
    "MirrorManager",
    "WorkspaceManager",
]
