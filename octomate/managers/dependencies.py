"""A tree's dependencies, installed once and inherited by every fork of it.

`uv`, `pnpm` and `npm` each keep one content-addressed store per machine and
hardlink out of it, so the second install of a package fetches nothing and costs
almost no disk. Nothing here isolates a tree from those stores and nothing
should: leaving them where the tools put them is the whole of why a fresh
workspace installs offline, and it is the constraint on anything that ever puts a
run behind a filesystem boundary.

The rest is one install per ecosystem, run where a tree just changed — in a
mirror, so a copied fork arrives ready to run, and in a fork, because a cloned one
carries no untracked file and so arrives with nothing installed at all.

Which ecosystem a tree belongs to is its lockfile's answer, and each manager below
owns the commands that follow from it. They differ in more than the binary's name:
uv needs two commands and a flag to produce an environment that survives being
copied, where the node managers need one and would be broken by the flag their own
documentation recommends.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

# Where a tree records the lockfile it was last installed from. Under `.git`,
# which is what lets a mirror and a fork share one call: `cp -a` brings the
# mirror's stamp along with the tree it describes, and `git clone` builds a fresh
# `.git` that has none.
STAMP = "octomate-installed"


class PackageManager(ABC):
    """One ecosystem's answer to "put this tree's dependencies in place".

    A manager is stateless and claims a tree by its lockfile, which is also what
    says whether the tree is still current. Everything a manager decides is in its
    `install`; the machinery around it — whether to install at all, and what to
    record afterwards — belongs to the module and is the same for all of them.
    """

    # The file whose presence makes a tree this manager's, and whose contents
    # decide whether what is installed is still what the tree asks for.
    lockfile: ClassVar[str]

    @abstractmethod
    async def install(self, tree: Path) -> bool:
        """Install `tree`'s dependencies, answering whether they are now there.

        The answer is what the stamp turns on, so a manager that reports success
        is promising the tree can be forked and used as it stands.
        """

    async def run(self, tree: Path, *argv: str) -> bool:
        """One command of an install, in `tree`, answering whether it worked.

        A failure is logged rather than raised. The tree is a working checkout
        either way and whatever runs there installs for itself, the way every run
        did before any of this existed — refusing someone a workspace because the
        host has no `pnpm` would turn a slow first turn into no turn at all.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=tree,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode == 0:
                return True
            reason = stderr.decode(errors="replace").strip()
        except OSError as error:
            reason = str(error)
        logger.warning(
            "`%s` in %s failed; the tree is left uninstalled and whatever runs "
            "there installs it: %s",
            " ".join(argv),
            tree,
            reason,
        )
        return False


class UV(PackageManager):
    """Python, through uv."""

    lockfile: ClassVar[str] = "uv.lock"

    async def install(self, tree: Path) -> bool:
        """Build the environment relocatable, then fill it.

        The venv is otherwise the one thing a fork cannot inherit: uv writes an
        absolute interpreter path into every console script, so a copied
        `.venv/bin/<tool>` keeps running the mirror's python against the mirror's
        site-packages. Nothing errors — the workspace simply is not the
        environment in use — and re-syncing does not repair it, since the copy
        already satisfies the lockfile and uv has nothing to do.

        Made relocatable, the scripts resolve the interpreter beside themselves
        and the copy is correct. uv records the choice in `pyvenv.cfg` and later
        syncs keep it, so this is the one moment it can be asked for.
        """
        return await self.run(tree, "uv", "venv", "--relocatable") and await self.run(
            tree, "uv", "sync"
        )


class Pnpm(PackageManager):
    """Node, through pnpm."""

    lockfile: ClassVar[str] = "pnpm-lock.yaml"

    async def install(self, tree: Path) -> bool:
        """`node_modules` is a tree of links into the store and copies cleanly, so
        there is nothing here but the install."""
        return await self.run(tree, "pnpm", "install")


class Npm(PackageManager):
    """Node, through npm."""

    lockfile: ClassVar[str] = "package-lock.json"

    async def install(self, tree: Path) -> bool:
        """`install` rather than the `ci` a lockfile would normally call for: `ci`
        begins by deleting `node_modules`, which is exactly the tree a warm mirror
        exists to keep."""
        return await self.run(tree, "npm", "install")


# In the order a tree is asked about, so a repository carrying two lockfiles is
# answered by the first. One that needs the other gets a field when it turns up.
MANAGERS: tuple[PackageManager, ...] = (UV(), Pnpm(), Npm())


def manager(tree: Path) -> PackageManager | None:
    """Whose tree this is, or None where nobody claims it — a plain folder of
    documents, and the empty tree a thread in no project forks."""
    for candidate in MANAGERS:
        if (tree / candidate.lockfile).is_file():
            return candidate
    return None


async def install(tree: Path) -> None:
    """Install `tree`'s dependencies, unless its lockfile is still the one they
    were last installed from.

    That stamp is what makes one call right for both trees this is asked about. A
    mirror installs when its lockfile moves, which is the only time it needs to. A
    copied fork inherits the stamp along with the installed tree and does nothing;
    a cloned fork has neither and installs from the store the mirror warmed; and a
    fork checked out on a ref whose lockfile differs installs whichever way it was
    made.

    An install that failed leaves no stamp, so the next tree forked from this one
    tries again rather than inheriting the claim that it is done.
    """
    owner = manager(tree)
    if owner is None:
        return
    stamp = tree / ".git" / STAMP
    digest = hashlib.sha256((tree / owner.lockfile).read_bytes()).hexdigest()
    if stamp.is_file() and stamp.read_text() == digest:
        return
    if await owner.install(tree):
        stamp.write_text(digest)
