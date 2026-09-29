"""File uploads over an authenticated transcript stream, independent of the harness."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from io import BytesIO

from fastapi import UploadFile, WebSocket, WebSocketDisconnect
from octomate_protocol.stream import (
    SESSION_FILE,
    StreamEof,
    StreamFinalize,
    StreamHello,
    StreamLine,
    StreamSnapshotStart,
    StreamSnapshotStored,
    client_message_adapter,
)
from starlette.datastructures import Headers

from octomate.schemas.files import FileVariant


class FileTransferError(ValueError):
    """The peer violated the requested file transfer's framing or byte count."""


@dataclass
class SnapshotUpload:
    """One announced transcript and its buffered bytes."""

    request: StreamSnapshotStart
    file: UploadFile
    received: int = 0
    stored: FileVariant | None = None


@dataclass(eq=False)
class FileTransferSlot:
    """Upload state for one WebSocket; text frames remain the caller's to interpret.

    The caller supplies a persistence operation bound to the authenticated owner
    and destination. The peer supplies only the attached transcript's bytes.
    """

    websocket: WebSocket
    persist: Callable[[UploadFile, int], Awaitable[FileVariant]] | None
    filename: str
    content_type: str
    upload: SnapshotUpload | None = field(default=None, init=False)
    offset: int = 0
    prefix: bytes = b""  # The owner's durable prefix, fixed for this connection.

    async def finalize(self) -> None:
        """Ask the client to drain, upload its transcript, then send EOF."""
        await self.websocket.send_text(StreamFinalize().model_dump_json())

    async def receive(self) -> StreamHello | StreamLine | StreamEof:
        """Consume upload frames, returning ordinary transcript messages to the caller."""
        while True:
            frame = await self.websocket.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(frame.get("code", 1000))
            data = frame.get("bytes")
            if isinstance(data, bytes):
                await self.receive_chunk(data)
                continue
            text = frame.get("text")
            if not isinstance(text, str):
                raise FileTransferError("Expected a text or binary WebSocket message")
            message = client_message_adapter.validate_json(text)
            upload = self.upload
            if isinstance(message, StreamEof):
                if (
                    self.persist is not None
                    and self.offset != len(self.prefix)
                    and (upload is None or upload.stored is None)
                ):
                    raise FileTransferError(
                        "Stream ended before the transcript was stored"
                    )
                return message
            if upload is not None:
                raise FileTransferError(
                    "Transcript messages cannot follow snapshot start"
                )
            if isinstance(message, StreamSnapshotStart):
                if (
                    self.persist is None
                    or message.start != len(self.prefix)
                    or message.end != self.offset
                    or message.end <= message.start
                ):
                    raise FileTransferError(
                        "Snapshot must match the drained transcript"
                    )
                self.upload = SnapshotUpload(
                    request=message,
                    file=UploadFile(
                        BytesIO(),
                        filename=self.filename,
                        headers=Headers({"content-type": self.content_type}),
                    ),
                )
                continue
            if isinstance(message, StreamLine) and message.agent_id in {
                None,
                SESSION_FILE,
            }:
                self.offset = message.end
            return message

    async def receive_chunk(self, data: bytes) -> None:
        """Persist exactly the announced bytes before acknowledging their storage."""
        upload = self.upload
        if self.persist is None or upload is None:
            raise FileTransferError(
                "No snapshot upload was announced on this connection"
            )
        if upload.stored is not None:
            raise FileTransferError("Snapshot upload is no longer pending")
        size = upload.request.end - upload.request.start
        if not data or upload.received + len(data) > size:
            raise FileTransferError(
                "Snapshot bytes exceed the requested size or are empty"
            )
        await upload.file.write(data)
        upload.received += len(data)
        if upload.received != size:
            return
        upload.stored = await self.persist(upload.file, upload.request.start)
        await self.websocket.send_text(
            StreamSnapshotStored(
                transfer_id=upload.request.transfer_id
            ).model_dump_json()
        )

    async def close(self) -> None:
        """Release the upload buffer on disconnect."""
        upload = self.upload
        if upload is None:
            return
        await upload.file.close()
