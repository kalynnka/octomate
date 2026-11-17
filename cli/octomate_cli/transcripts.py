"""Upload a transcript from the machine running an agent harness."""

import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

import typer
from octomate_protocol.stream import TranscriptKept

from octomate_cli.config import CLISettings, cli_settings

UPLOAD_TIMEOUT = 60


def post_transcript(url: str, token: str, offset: int, data: bytes) -> int:
    """Send bytes from `offset` and return the server's retained byte count."""
    request = urllib.request.Request(
        f"{url}?offset={offset}",
        data=data,
        headers={
            "Content-Type": "application/jsonl",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:
            return TranscriptKept.model_validate_json(response.read()).size
    except urllib.error.HTTPError as error:
        typer.secho(
            f"octomate: {url} refused the transcript from byte {offset}: "
            f"{error.code} {error.read().decode()}",
            err=True,
        )
        raise typer.Exit(1) from None


def upload_file(conversation: UUID, path: Path, *, hook_path: str) -> None:
    """Upload new complete transcript lines in one request; silent on success."""
    settings = cli_settings()
    if settings.url is None or settings.token is None:
        raise typer.BadParameter(
            f"${CLISettings.env('url')} and ${CLISettings.env('token')} must say "
            "where Octomate is and who is uploading"
        )
    url = f"{settings.url.rstrip('/')}{hook_path}/transcripts/{conversation}"
    kept = post_transcript(url, settings.token, 0, b"")
    if kept > path.stat().st_size:
        typer.secho(
            f"octomate: the server keeps {kept} bytes of the conversation's "
            f"transcript, more than {path} holds",
            err=True,
        )
        raise typer.Exit(1)
    with path.open("rb") as transcript:
        transcript.seek(kept)
        rest = transcript.read()
    # A line still being written stays for the next upload.
    rest = rest[: rest.rfind(b"\n") + 1]
    if rest:
        post_transcript(url, settings.token, kept, rest)
