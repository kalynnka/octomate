"""`DeepseekProcess` against a stand-in `dsh` binary: the arg vector it spawns,
and the `--no-open` negotiation — offered on the first launch, dropped and
retried only when *that* flag is the one this dsh refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import HttpUrl

from octomate.tentacles.agents.deepseek.process import (
    NO_OPEN,
    DeepseekProcess,
    HarnessOptionUnsupportedError,
)

PORT = 3080
BASE_URL = HttpUrl(f"http://127.0.0.1:{PORT}")


def fake_dsh(tmp_path: Path, refuses: str | None = None) -> tuple[Path, Path]:
    """A stand-in `dsh` that appends each launch's argv to a file, prints the
    readiness banner and stays up. Given `refuses`, it instead answers that flag
    the way a `dsh web` too old for it does: commander's one refusal line on
    stderr, then exit. `exec` hands the process to `sleep`, so a SIGTERM from
    `stop` lands on the process that holds the pipes."""
    binary = tmp_path / "dsh"
    argv = tmp_path / "argv.txt"
    refusal = (
        ""
        if refuses is None
        else f"""for arg in "$@"; do
  if [ "$arg" = "{refuses}" ]; then
    echo "error: unknown option '{refuses}'" >&2
    exit 1
  fi
done
"""
    )
    binary.write_text(
        f'#!/bin/sh\necho "$@" >> {argv}\n'
        f"{refusal}"
        f'echo "dsh web: http://127.0.0.1:{PORT}"\n'
        "exec sleep 30\n"
    )
    binary.chmod(0o755)
    return binary, argv


def launches(argv: Path) -> list[list[str]]:
    return [line.split() for line in argv.read_text().splitlines()]


def process(binary: Path, tmp_path: Path, extra_args: list[str]) -> DeepseekProcess:
    return DeepseekProcess(
        executable=str(binary),
        port=PORT,
        extra_args=extra_args,
        dsh_home=tmp_path / "home",
        ready_timeout=10.0,
    )


async def test_launches_on_loopback_with_no_open_before_extra_args(
    tmp_path: Path,
) -> None:
    binary, argv = fake_dsh(tmp_path)
    dsh = process(binary, tmp_path, ["--patch", "overlay.yml"])

    try:
        assert await dsh.start() == BASE_URL
    finally:
        await dsh.stop()

    assert launches(argv) == [
        [
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            NO_OPEN,
            "--patch",
            "overlay.yml",
        ]
    ]


async def test_a_dsh_refusing_no_open_is_started_again_without_it(
    tmp_path: Path,
) -> None:
    binary, argv = fake_dsh(tmp_path, refuses=NO_OPEN)
    dsh = process(binary, tmp_path, [])

    try:
        assert await dsh.start() == BASE_URL
    finally:
        await dsh.stop()

    first, second = launches(argv)
    assert first == ["web", "--host", "127.0.0.1", "--port", str(PORT), NO_OPEN]
    assert second == ["web", "--host", "127.0.0.1", "--port", str(PORT)]


async def test_a_refused_extra_arg_fails_the_start_without_a_retry(
    tmp_path: Path,
) -> None:
    binary, argv = fake_dsh(tmp_path, refuses="--patch")
    dsh = process(binary, tmp_path, ["--patch"])

    with pytest.raises(HarnessOptionUnsupportedError) as caught:
        await dsh.start()

    # Dropping our own flag would not fix the operator's, so the refusal is
    # reported rather than hidden behind a second identical failure.
    assert caught.value.option == "--patch"
    assert len(launches(argv)) == 1


async def test_a_dsh_that_dies_without_refusing_anything_fails_the_start(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "dsh"
    binary.write_text('#!/bin/sh\necho "boom" >&2\nexit 3\n')
    binary.chmod(0o755)
    dsh = process(binary, tmp_path, [])

    with pytest.raises(RuntimeError, match="exited before reporting a URL"):
        await dsh.start()
