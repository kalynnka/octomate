"""Aggregate configuration for the enabled agents."""

from __future__ import annotations

from functools import cached_property
from typing import Self

from pydantic import BaseModel, model_validator

from octomate.config.agents.claude import ClaudeCodeConfig
from octomate.config.agents.codex import CodexConfig
from octomate.config.agents.common import AgentConfig
from octomate.config.agents.deepseek import DeepseekConfig
from octomate.config.agents.inkling import InklingConfig
from octomate.config.agents.zcode import ZcodeConfig


class AgentsConfig(BaseModel):
    """Opt-in agents. External harnesses own their model catalogs and defaults."""

    inkling: InklingConfig | None = None
    claude: ClaudeCodeConfig | None = None
    codex: CodexConfig | None = None
    deepseek: DeepseekConfig | None = None
    zcode: ZcodeConfig | None = None

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        seen: set[str] = set()
        for field_name in type(self).model_fields:
            agent = getattr(self, field_name)
            if isinstance(agent, AgentConfig):
                if agent.id in seen:
                    raise ValueError(f"duplicate agent id {agent.id!r}")
                seen.add(agent.id)
        return self

    @cached_property
    def configured_agents(self) -> list[AgentConfig]:
        agents: list[AgentConfig] = []
        for field_name in type(self).model_fields:
            agent = getattr(self, field_name)
            if isinstance(agent, AgentConfig) and agent.enabled:
                agents.append(agent)
        return agents
