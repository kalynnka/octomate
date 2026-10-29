"""Native temporary storage stays usable without exposing shared temporary files."""

import asyncio
import re
from pathlib import Path
from shlex import quote

import anyio
import pytest

from tests.integration.native_sandbox.matrix import NativeCall, SandboxTree


@pytest.mark.parametrize("native_call", ["claude"], indirect=True)
async def test_claude_private_temp_is_usable_and_removed(
    native_call: NativeCall, sandbox_tree: SandboxTree
) -> None:
    source = quote(str(sandbox_tree.shared / "canary.txt"))
    control = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        f"./bin/report {source}",
        cwd=sandbox_tree.workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await control.communicate()
    assert control.returncode == 0, stderr.decode()
    assert sandbox_tree.marker in stdout.decode()
    command = (
        'test -n "${TMPDIR:-}" && '
        'native_temp_file=$(mktemp "$TMPDIR/native-probe.XXXXXX") && '
        'printf NATIVE_TEMP_OK > "$native_temp_file" && '
        'cat "$native_temp_file" && rm "$native_temp_file" && '
        'printf "\\nNATIVE_TMP=%s\\n" "$TMPDIR"; '
        f"if ./bin/report {source} > /dev/null 2>&1; "
        "then printf NATIVE_SHARED_LEAK; else printf NATIVE_SHARED_DENIED; fi"
    )
    outcome = await native_call("shell", command)
    assert "NATIVE_TEMP_OK" in outcome.text, outcome.text
    assert "NATIVE_SHARED_DENIED" in outcome.text, outcome.text
    assert "NATIVE_SHARED_LEAK" not in outcome.text, outcome.text
    assert "operation not permitted" not in outcome.text.lower(), outcome.text
    match = re.search(r"^NATIVE_TMP=(.+)$", outcome.text, re.MULTILINE)
    assert match is not None, outcome.text
    temp = Path(match[1])
    assert temp.is_absolute()
    assert any(part.startswith("octo-") for part in temp.parts)
    assert not await anyio.Path(temp).exists()
