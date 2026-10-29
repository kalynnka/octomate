"""DSH keeps its native write boundary without breaking temporary storage."""

import asyncio
import json
import os
from pathlib import Path

import pytest
import yaml

from octomate.tentacles.deepseek.process import DeepseekProcess
from tests.integration.native_sandbox.matrix import NativeCall


async def test_deepseek_managed_config_overrides_unsafe_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if executable is None:
        pytest.skip("set DSH_TEST_EXECUTABLE to run the native DSH integration")
    monkeypatch.setenv("DSH_PERMISSION_MODE", "danger-full-access")
    monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp_path / "agents"))
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
    overlay = tmp_path / "unsafe.json"
    overlay.write_text(
        json.dumps(
            [
                {
                    "id": "sandbox",
                    "config": {
                        "runnerCommand": ["missing-sandbox-runner"],
                        "runnerFailureSignatures": ["broken runner"],
                    },
                },
                {"id": "bash-sandbox", "name": "@deepseek-ai/dsh-bash-local"},
                {"id": "pwsh-sandbox", "name": "@deepseek-ai/dsh-pwsh-local"},
                {"id": "fs-sandbox", "name": "@deepseek-ai/dsh-fs-local"},
                {"id": "sandbox-policy", "config": {"mode": "danger-full-access"}},
                {"id": "approval", "config": {"policy": "never"}},
                {
                    "id": "permission",
                    "config": {
                        "presets": {
                            "workspace-write": {
                                "sandbox": "danger-full-access",
                                "approval": "never",
                            }
                        }
                    },
                },
                {"id": "hmr", "disabled": False},
                {"id": "plugin-manager", "disabled": False},
                {"id": "tool-plugin-manager", "disabled": False},
            ]
        )
    )
    dsh = DeepseekProcess(
        executable=executable,
        port=0,
        extra_args=["--patch", str(overlay)],
        dsh_home=tmp_path / "home",
        ready_timeout=60,
    )
    try:
        await dsh.start()
        assert dsh.runtime_home is not None
        runtime_home = dsh.runtime_home.name
        dump = await asyncio.create_subprocess_exec(
            executable,
            "web",
            "--patch",
            str(overlay),
            "--patch",
            str(Path(runtime_home) / "driven.json"),
            "--dump-config",
            env={**dsh.child_environment(), "DSH_HOME": runtime_home},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(dump.communicate(), 30)
        assert dump.returncode == 0, stderr.decode()
        # BaseLoader preserves unevaluated native !!js fields in unrelated rows.
        plugins = {
            entry["id"]: entry for entry in yaml.load(stdout, Loader=yaml.BaseLoader)
        }
        assert plugins["sandbox"]["config"]["runnerCommand"] == []
        assert plugins["sandbox"]["config"]["runnerFailureSignatures"] == []
        assert plugins["sandbox-policy"]["config"]["mode"] == "workspace-write"
        assert plugins["approval"]["config"]["policy"] == "ask"
        preset = plugins["permission"]["config"]["presets"]["workspace-write"]
        assert preset == {"sandbox": "workspace-write", "approval": "ask"}
        for name in ("bash", "pwsh", "fs"):
            assert (
                plugins[f"{name}-sandbox"]["name"] == f"@deepseek-ai/dsh-{name}-sandbox"
            )
        assert plugins["bash-sandbox"]["disabled"] == str(os.name == "nt").lower()
        assert plugins["pwsh-sandbox"]["disabled"] == str(os.name != "nt").lower()
        assert plugins["fs-sandbox"]["disabled"] == "false"
        for name in ("hmr", "plugin-manager", "tool-plugin-manager"):
            assert plugins[name]["disabled"] == "true"
    finally:
        await dsh.stop()


@pytest.mark.parametrize("native_call", ["deepseek"], indirect=True)
async def test_deepseek_native_temp_is_usable(native_call: NativeCall) -> None:
    outcome = await native_call(
        "shell",
        "native_temp_file=$(mktemp) && "
        'printf NATIVE_TEMP_OK > "$native_temp_file" && '
        'cat "$native_temp_file" && rm "$native_temp_file"',
    )
    assert "NATIVE_TEMP_OK" in outcome.text, outcome.text
