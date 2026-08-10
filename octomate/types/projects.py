from __future__ import annotations

from typing import Literal

# Here rather than on either side because the ORM model and the schema both spell it
# out, and a divergence between the two is only found at runtime.

# What put this project in the registry: `declared` for one registered directly, or the
# native runtime whose session was found running in it. Stored rather than derived
# because it is the only part of a discovered project that cannot be recovered later —
# whether a root is a git repository, or is still on disk, is answerable at any time,
# but which runtime first named it as a workspace is not.
#
# Codex and not Claude, though both runtimes have something they call a project: Codex's
# is a workspace, a set of directories a session may work in, which is this project.
# Claude's is a context store — `~/.claude/projects/<slug>` groups a directory's
# transcripts and memory, and says nothing about a tree being worked in. So a Claude
# session is filed under a project that already holds its directory and registers none;
# its directory is recorded on the run either way, and can be promoted later.
ProjectOrigin = Literal["declared", "codex"]
