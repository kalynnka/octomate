"""`DeepseekProcess` against a stand-in `dsh` binary: the arg vector it spawns,
and the `--no-open` negotiation — offered on the first launch, dropped and
retried only when *that* flag is the one this dsh refuses."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import HttpUrl

from octomate.tentacles.deepseek.process import (
    NO_OPEN,
    DeepseekProcess,
    HarnessOptionUnsupportedError,
)

PORT = 3080
BASE_URL = HttpUrl(f"http://127.0.0.1:{PORT}")


def fake_dsh(
    tmp_path: Path, refuses: str | None = None, token: str | None = None
) -> tuple[Path, Path]:
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
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo 0.1.6-alpha.1; exit; fi\n'
        f'echo "$@" >> {argv}\n'
        f"{refusal}"
        f'echo "dsh web: http://127.0.0.1:{PORT}{"/?token=" + token if token else ""}"\n'
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


async def test_launches_with_patch_before_web_app_options(
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
            "--patch",
            "overlay.yml",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            NO_OPEN,
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


@pytest.mark.parametrize("browser_url", [None, "https://dsh.example:8443"])
async def test_launch_url_is_printed_once_without_writing_a_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    browser_url: str | None,
) -> None:
    token = "private-launch-token"
    binary, argv = fake_dsh(tmp_path, token=token)
    dsh = process(binary, tmp_path, [])
    dsh.browser_url = HttpUrl(browser_url) if browser_url is not None else None
    caplog.set_level(logging.DEBUG, logger="octomate.tentacles.deepseek.process")

    try:
        assert await dsh.start() == BASE_URL
        assert dsh.launch_token is not None
        assert dsh.launch_token.get_secret_value() == token
        assert token not in repr(dsh)
        assert token not in caplog.text
        output = capsys.readouterr().out
        assert output.startswith("dsh web: ")
        assert output.count(token) == 1
        link = urlsplit(output.strip().removeprefix("dsh web: "))
        assert link.netloc == urlsplit(browser_url or str(BASE_URL)).netloc
        assert parse_qs(link.query) == {"token": [token]}
        assert not dsh.dsh_home.exists()
        dsh.capture_diagnostic(f"repeated launch URL: {output.strip()}")
        assert token not in caplog.text
        assert capsys.readouterr().out == ""
        if browser_url is not None:
            args = launches(argv)[0]
            assert args[args.index("--trusted-host") + 1] == link.netloc
    finally:
        await dsh.stop()
    assert not dsh.dsh_home.exists()
    assert dsh.launch_token is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.6-alpha.1", "matches Octomate's tested release"),
        ("0.2.0", "differs from Octomate's tested version"),
        ("unknown", "Could not determine dsh version"),
    ],
)
async def test_version_matching_is_visible(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    version: str,
    expected: str,
) -> None:
    binary = tmp_path / "dsh"
    binary.write_text(f"#!/bin/sh\necho '{version}'\n")
    binary.chmod(0o755)
    caplog.set_level(logging.INFO)
    await process(binary, tmp_path, []).check_version()
    assert expected in caplog.text


async def test_startup_failure_keeps_cause_without_flooding_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    binary = tmp_path / "dsh"
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo 0.1.6-alpha.1; exit; fi\n'
        'echo "Cannot find module required-plugin" >&2\n'
        'echo "http://127.0.0.1/?token=private-token" >&2\n'
        'i=0; while [ "$i" -lt 300 ]; do echo "    at frame-$i" >&2; i=$((i+1)); done\n'
        "exit 1\n"
    )
    binary.chmod(0o755)
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError) as caught:
        await process(binary, tmp_path, []).start()
    message = str(caught.value)
    assert "Cannot find module required-plugin" in message
    assert "frame-299" in message
    assert "omitted" in message
    assert len(message.splitlines()) <= 31
    assert "private-token" not in message + caplog.text
    assert len(caplog.records) <= 2
