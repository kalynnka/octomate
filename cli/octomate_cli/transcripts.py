"""Upload transcripts from the machine running an agent harness."""

import asyncio
from pathlib import Path
from uuid import UUID

import httpx2
import typer
from octomate_protocol.stream import TranscriptOffsets

from octomate_cli.config import CLISettings, cli_settings

UPLOAD_TIMEOUT = 60


def complete_lines(path: Path, offset: int) -> bytes:
    """The file's complete lines from `offset`. A line still being written stays
    for the next upload."""
    with path.open("rb") as transcript:
        if transcript.seek(0, 2) < offset:
            typer.secho(
                f"octomate: the server keeps {offset} bytes of the conversation's "
                f"transcript, more than {path} holds",
                err=True,
            )
            raise typer.Exit(1)
        transcript.seek(offset)
        rest = transcript.read()
    return rest[: rest.rfind(b"\n") + 1]


async def upload_transcripts(
    conversation: UUID,
    path: Path | None,
    *,
    hook_path: str,
    subagents: dict[str, Path] | None = None,
) -> None:
    """Upload each file's new complete lines, all files at once; silent on success.

    `path` is the conversation's own transcript and `subagents` maps a subagent's
    id to its file. One request asks where every file resumes, then each file
    goes up in a request of its own.
    """
    settings = cli_settings()
    if settings.url is None or settings.token is None:
        raise typer.BadParameter(
            f"${CLISettings.env('url')} and ${CLISettings.env('token')} must say "
            "where Octomate is and who is uploading"
        )
    url = f"{settings.url.rstrip('/')}{hook_path}/transcripts/{conversation}"
    children = subagents or {}
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=UPLOAD_TIMEOUT,
        ) as client:
            response = await client.post(f"{url}/offsets", json=list(children))
            response.raise_for_status()
            kept = TranscriptOffsets.model_validate_json(response.content)
            files = {
                id_: children[name]
                for id_, name in kept.subagents.items()
                if name in children
            }
            if path is not None:
                files[conversation] = path

            async def upload(transcript_id: UUID, path: Path) -> None:
                offset = kept.offsets[transcript_id]
                data = await asyncio.to_thread(complete_lines, path, offset)
                if data:
                    response = await client.post(
                        url,
                        params={"offset": offset, "transcript_id": str(transcript_id)},
                        headers={"Content-Type": "application/jsonl"},
                        content=data,
                    )
                    response.raise_for_status()

            await asyncio.gather(*(upload(id_, file) for id_, file in files.items()))
    except httpx2.HTTPStatusError as error:
        typer.secho(
            f"octomate: {error.request.url} refused the transcript: "
            f"{error.response.status_code} {error.response.text}",
            err=True,
        )
        raise typer.Exit(1) from None
