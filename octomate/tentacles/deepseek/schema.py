"""Configuration supplied to the DeepSeek harness process."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, RootModel
from typing_extensions import TypedDict


class FileStorageConfig(TypedDict):
    path: Path


class FileStoragePatch(TypedDict):
    id: Literal["settings", "credentials"]
    config: FileStorageConfig


class SessionStorageConfig(TypedDict):
    root: Path


class SessionStoragePatch(TypedDict):
    id: Literal["session-persistence-jsonl"]
    config: SessionStorageConfig


class AttachmentStorageConfig(TypedDict):
    dshHome: Path


class AttachmentStoragePatch(TypedDict):
    id: Literal["attachment-local"]
    config: AttachmentStorageConfig


class SandboxRunnerConfig(TypedDict):
    runnerCommand: list[str]
    runnerFailureSignatures: list[str]


class SandboxRunnerPatch(TypedDict):
    id: Literal["sandbox"]
    name: Literal["@deepseek-ai/dsh-sandbox-local"]
    disabled: Literal[False]
    config: SandboxRunnerConfig


class SandboxPolicyConfig(TypedDict):
    mode: Literal["workspace-write"]


class SandboxPolicyPatch(TypedDict):
    id: Literal["sandbox-policy"]
    name: Literal["@deepseek-ai/dsh-sandbox-policy"]
    disabled: Literal[False]
    config: SandboxPolicyConfig


class SandboxedBackendPatch(TypedDict):
    id: Literal["bash-sandbox", "pwsh-sandbox", "fs-sandbox"]
    name: Literal[
        "@deepseek-ai/dsh-bash-sandbox",
        "@deepseek-ai/dsh-pwsh-sandbox",
        "@deepseek-ai/dsh-fs-sandbox",
    ]
    disabled: bool


class ApprovalConfig(TypedDict):
    policy: Literal["ask"]


class ApprovalPatch(TypedDict):
    id: Literal["approval"]
    name: Literal["@deepseek-ai/dsh-user-approval"]
    disabled: Literal[False]
    config: ApprovalConfig


class PermissionPreset(TypedDict):
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"]
    approval: Literal["ask", "never"]


class PermissionConfig(TypedDict):
    presets: dict[
        Literal["read-only", "workspace-write", "danger-full-access"],
        PermissionPreset,
    ]


class PermissionPatch(TypedDict):
    id: Literal["permission"]
    name: Literal["@deepseek-ai/dsh-permission-presets"]
    disabled: Literal[False]
    config: PermissionConfig


class DisabledPluginPatch(TypedDict):
    id: Literal["hmr", "plugin-manager", "tool-plugin-manager"]
    disabled: Literal[True]


class DrivenConfigPatch(
    RootModel[
        list[
            Annotated[
                FileStoragePatch
                | SessionStoragePatch
                | AttachmentStoragePatch
                | SandboxRunnerPatch
                | SandboxPolicyPatch
                | SandboxedBackendPatch
                | ApprovalPatch
                | PermissionPatch
                | DisabledPluginPatch,
                Field(discriminator="id"),
            ]
        ]
    ]
):
    pass
