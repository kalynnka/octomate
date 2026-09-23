"""Configuration written or inspected by the DeepSeek hook installer."""

from typing import Literal

from pydantic import BaseModel
from typing_extensions import TypedDict


class CommandHook(TypedDict):
    type: Literal["command"]
    command: str
    timeout: int


class HookGroup(TypedDict):
    hooks: list[CommandHook]


class HookSettings(BaseModel):
    hooks: dict[Literal["UserPromptSubmit", "Stop"], list[HookGroup]]


class BridgeManifest(BaseModel):
    name: str
