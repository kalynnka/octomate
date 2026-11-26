"""Run DSH in a private home until its attached supervisor closes stdin."""

import asyncio
import os
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


async def run() -> int:
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            # UDP connect selects the routed interface without sending a packet.
            route.connect(("192.0.2.1", 9))
            host = route.getsockname()[0]
        with TemporaryDirectory(prefix="octomate-dsh-") as home:
            patch = Path(home) / "driven.json"
            patch.write_bytes(await reader.readline())
            # DSH consumes CLI patches before passing web options to its app.
            boundary = sys.argv.index("--")
            process = await asyncio.create_subprocess_exec(
                *sys.argv[1:boundary],
                "--patch",
                str(patch),
                "--host",
                host,
                *sys.argv[boundary + 1 :],
                stdin=asyncio.subprocess.DEVNULL,
                env={**os.environ, "DSH_HOME": home},
            )
            return await supervise(process, reader)
    finally:
        transport.close()


async def supervise(
    process: asyncio.subprocess.Process, reader: asyncio.StreamReader
) -> int:
    """Reap the child on supervisor EOF, or preserve its own exit status."""
    closed = asyncio.create_task(reader.read())
    exited = asyncio.create_task(process.wait())
    try:
        await asyncio.wait((closed, exited), return_when=asyncio.FIRST_COMPLETED)
        if exited.done():
            return exited.result()
        return 0
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(exited), 3)
            except TimeoutError:
                process.kill()
        await exited
        closed.cancel()
        await asyncio.gather(closed, return_exceptions=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
