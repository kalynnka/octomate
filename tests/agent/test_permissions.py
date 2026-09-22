from __future__ import annotations

from typing import get_args

import pytest
from claude_agent_sdk import PermissionMode as ClaudePermissionMode
from pydantic import ValidationError

from octomate import Octomate
from octomate.config.agents import CodexConfig, DeepseekConfig
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.wire import ErrResult, OkResult, RpcError
from tests.agent.test_deepseek_tentacle import FakeDeepseekApi, patch_gateway


async def test_permission_discovery_keeps_custom_names_and_descriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["permissionPresets/catalog"] = OkResult(
        value={
            "options": [
                {
                    "value": "audit-only",
                    "name": "Audit",
                    "description": "Read without editing.",
                },
                {"value": "workspace-write", "name": "Workspace"},
            ]
        }
    )
    tentacle = DeepseekTentacle(
        "auditor", Octomate(), config=DeepseekConfig(permission_mode="audit-only")
    )
    async with tentacle:
        assert [mode.value for mode in tentacle.permission_modes] == [
            "audit-only",
            "workspace-write",
        ]
        assert tentacle.info.permission_modes[0].name == "Audit"
        assert tentacle.info.permission_modes[0].description == "Read without editing."
        assert tentacle.info.default_permission_mode == "audit-only"
        tentacle.check_permission_mode("audit-only")
        with pytest.raises(ValueError, match="not one of auditor's modes"):
            tentacle.check_permission_mode("bypassPermissions")


@pytest.mark.parametrize("options", [[], [{"value": "audit-only", "name": "Audit"}]])
async def test_permission_discovery_rejects_an_unavailable_default(
    monkeypatch: pytest.MonkeyPatch, options: list[dict[str, str]]
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["permissionPresets/catalog"] = OkResult.model_validate(
        {"value": {"options": options}}
    )
    tentacle = DeepseekTentacle("deepseek", Octomate(), config=DeepseekConfig())
    with pytest.raises(ValueError, match="not one of deepseek's modes"):
        async with tentacle:
            pytest.fail("An unavailable default must fail startup")


async def test_permission_discovery_failure_is_not_replaced_with_a_fixed_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["permissionPresets/catalog"] = ErrResult(
        error=RpcError(code="unavailable", message="No permission service")
    )
    tentacle = DeepseekTentacle("deepseek", Octomate(), config=DeepseekConfig())
    with pytest.raises(RuntimeError, match="No permission service"):
        async with tentacle:
            pytest.fail("Discovery errors must fail startup")


def test_claude_catalog_uses_the_sdk_permission_vocabulary() -> None:
    assert tuple(
        mode.value for mode in ClaudeCodeTentacle.permission_modes
    ) == get_args(ClaudePermissionMode)


def test_codex_catalog_exposes_the_three_ui_presets() -> None:
    assert [(mode.value, mode.name) for mode in CodexTentacle.permission_modes] == [
        ("user_review", "Ask for approval"),
        ("auto_review", "Approve for me"),
        ("full_access", "Full access"),
    ]


@pytest.mark.parametrize("mode", ["user_review", "auto_review", "full_access"])
def test_codex_config_preserves_preset_ids(mode: str) -> None:
    config = CodexConfig.model_validate({"permission_mode": mode})
    assert config.permission_mode == mode
    assert config.model_dump(mode="json")["permission_mode"] == mode


def test_old_deny_all_config_requires_an_explicit_preset_choice() -> None:
    with pytest.raises(ValidationError, match="permission_mode"):
        CodexConfig.model_validate({"permission_mode": "deny_all"})


@pytest.mark.parametrize("sandbox", ["read_only", "workspace_write", "full_access"])
def test_separate_sandbox_config_is_not_silently_ignored(sandbox: str) -> None:
    with pytest.raises(ValidationError, match="sandbox is now part of permission_mode"):
        CodexConfig.model_validate({"sandbox": sandbox})
