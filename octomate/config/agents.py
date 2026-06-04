from __future__ import annotations

from pydantic import BaseModel, Field

from octomate.config.models import ModelConfig


class InklingConfig(BaseModel):
    model: ModelConfig = ModelConfig(
        provider="vertex",
        name="gemini-3-flash-preview",
        settings={"thinking": "medium"},
    )


class AgentsConfig(BaseModel):
    inkling: InklingConfig = Field(default_factory=InklingConfig)
