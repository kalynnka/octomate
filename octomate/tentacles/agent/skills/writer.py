from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octomate.tentacles.agent.skills.library import SkillLibrary

logger = logging.getLogger(__name__)

ALLOWED_SUBDIRS = {"references", "scripts", "assets"}


def _validate_name(name: str) -> str | None:
    if not name:
        return "name must not be empty"
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", name):
        return f"name {name!r} must be lowercase alphanumeric/dash/dot (max 64 chars)"
    return None


def _validate_rel_path(rel: str) -> str | None:
    parts = Path(rel).parts
    if not parts:
        return "file_path must not be empty"
    if parts[0] == "SKILL.md":
        return None  # root SKILL.md is always allowed
    if len(parts) < 2 or parts[0] not in ALLOWED_SUBDIRS:
        return f"file_path must be SKILL.md or under {ALLOWED_SUBDIRS!r}"
    if ".." in parts:
        return "file_path must not traverse up"
    return None


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AtomicSkillWriter:
    """All-or-nothing writer for skill directories.

    Every ``commit()`` call either lands all files together or leaves the
    existing skill directory untouched.  Uses per-skill lock files and a
    staging-dir swap pattern to achieve atomicity on POSIX.
    """

    def __init__(self, user_root: Path) -> None:
        self.user_root = user_root

    def _lock_path(self, name: str) -> Path:
        locks = self.user_root / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        return locks / f"{name}.lock"

    def _staging_path(self, name: str) -> Path:
        staging_base = self.user_root / ".staging"
        staging_base.mkdir(parents=True, exist_ok=True)
        return staging_base / f"{name}-{uuid.uuid4().hex}"

    def _trash_path(self, name: str) -> Path:
        trash = self.user_root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        return trash / f"{name}-{ts}"

    def commit(
        self,
        name: str,
        files: Mapping[str, bytes | None],
        *,
        seed_from: Path | None = None,
        library: SkillLibrary | None = None,
    ) -> None:
        """Write ``files`` to the skill directory for ``name`` atomically.

        ``files`` maps skill-relative paths (``"SKILL.md"``,
        ``"references/api.md"``, …) to byte content.  ``None`` means delete
        the file from the final state.  Missing files are copied from the
        current skill directory on disk (if any).

        ``seed_from`` pins the source directory for seeding; if omitted, the
        writer looks in ``user_root/<name>`` and then any root in the library.

        After a successful commit, ``library.reload()`` is called so callers
        see the new skill immediately.
        """
        err = _validate_name(name)
        if err:
            raise ValueError(err)
        for rel in files:
            err = _validate_rel_path(rel)
            if err:
                raise ValueError(f"invalid path {rel!r}: {err}")

        lock_path = self._lock_path(name)
        staging = self._staging_path(name)
        target = self.user_root / name

        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                self._build_staging(staging, name, files, seed_from, library)
                self._swap(staging, target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

        if library is not None:
            library.reload()
        logger.info(
            "skills: committed %d file(s) to %s/%s", len(files), self.user_root, name
        )

    def _build_staging(
        self,
        staging: Path,
        name: str,
        files: Mapping[str, bytes | None],
        seed_from: Path | None,
        library: SkillLibrary | None,
    ) -> None:
        staging.mkdir(parents=True, exist_ok=True)

        # Determine seed source — copy existing content we're not overwriting
        source = seed_from or self._find_existing(name, library)
        if source is not None and source.is_dir():
            for src_file in source.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(source)
                rel_str = str(rel)
                if rel_str in files:
                    continue  # will be overwritten
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dest)

        # Write the explicitly listed files (None = delete = skip copying)
        for rel_str, content in files.items():
            if content is None:
                continue  # explicitly deleted — don't stage it
            dest = staging / rel_str
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Atomic per-file write inside the staging dir
            fd, tmp_path = tempfile.mkstemp(dir=dest.parent)
            try:
                os.write(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, dest)

        # fsync all written files and the staging directory itself
        for f in staging.rglob("*"):
            if f.is_file():
                with open(f, "rb") as fh:
                    os.fsync(fh.fileno())
        _fsync_dir(staging)

    def _find_existing(self, name: str, library: SkillLibrary | None) -> Path | None:
        candidate = self.user_root / name
        if candidate.is_dir():
            return candidate
        if library is not None:
            return library.find_in_any_root(name)
        return None

    def _swap(self, staging: Path, target: Path) -> None:
        if target.exists():
            trash = self._trash_path(target.name)
            os.rename(target, trash)
        os.rename(staging, target)
        _fsync_dir(target.parent)

    def delete(self, name: str, *, library: SkillLibrary | None = None) -> None:
        """Move ``user_root/<name>`` to trash (recoverable delete)."""
        err = _validate_name(name)
        if err:
            raise ValueError(err)
        target = self.user_root / name
        if not target.is_dir():
            raise FileNotFoundError(f"skill {name!r} not found under user root")
        lock_path = self._lock_path(name)
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            trash = self._trash_path(name)
            os.rename(target, trash)
            _fsync_dir(target.parent)
        if library is not None:
            library.reload()
        logger.info("skills: deleted %s → %s", name, trash)
