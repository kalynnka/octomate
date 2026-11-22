"""Shared probes and cleanup for disposable local sandbox acceptance."""

from octomate.habitat.sbx import SbxHabitat


async def remove_sandbox(sandbox: SbxHabitat) -> None:
    names = (await sandbox.command("ls", quiet=True)).decode().splitlines()
    if sandbox.name in names:
        await sandbox.command("rm", "--force", sandbox.name)


async def cli_pids(
    sandbox: SbxHabitat, *, argument_pattern: str = "--output-forma[t]"
) -> list[int]:
    paths = await sandbox.command(
        "exec",
        sandbox.name,
        "find",
        "/proc",
        "-maxdepth",
        "2",
        "-name",
        "cmdline",
        "-exec",
        "grep",
        "-lzx",
        "-e",
        # Match the CLI argument without matching this probe itself.
        argument_pattern,
        "{}",
        ";",
    )
    return [int(path.split("/")[2]) for path in paths.decode().splitlines()]
