"""Composing the configured inkling agent into its tentacle.

The counterpart to `build_channel`, and it does more than dispatch because inkling is
the one agent assembled rather than handed a config: its models come from the provider
registry and its capability stack from its own config. That assembly is the thing
worth having in one place — a launcher should ask for the tentacle, not know how one
is made. The providers — Slack, Linear, a vendor's server — reach it per run, from
the host's tentacles, not from here.

Imports sit inside the function because the capability stack pulls in pydantic-ai's
harness, and importing this module must not cost that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from octomate.config.agents import (
    InklingConfig,
    SpillAction,
    SummarizeAction,
    TruncateAction,
)
from octomate.tentacles.inkling.base import InklingTentacle

if TYPE_CHECKING:
    from octomate import Octomate
    from octomate.providers import ProviderRegistry


def build_inkling(
    id: str,
    config: InklingConfig,
    octomate: Octomate,
    *,
    registry: ProviderRegistry,
) -> InklingTentacle:
    """The inkling tentacle, with its models and capability stack.

    `registry` builds the models rather than pydantic-ai resolving their names, so a
    model here means what `providers.yaml` says it means.
    """
    from pydantic_ai import AgentCapability
    from pydantic_ai_harness.tool_output_limits import (
        Band,
        Spill,
        Summarize,
        ToolOutputLimits,
        Truncate,
    )
    from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

    from octomate.capabilities.ask import AskCapability
    from octomate.capabilities.history import HistoryCapability
    from octomate.capabilities.todos import TodoCapability
    from octomate.capabilities.tools import ToolFailureCapability
    from octomate.managers.spills import SpillStore
    from octomate.tentacles.inkling.prompts import SYSTEM_PROMPT

    capabilities: list[AgentCapability[None]] = [
        # Observational only: it warns, it never edits a request. A collapsed prompt
        # cache is otherwise invisible — the run still succeeds, just slower and dearer.
        WarnOnCacheBusts(),
        ToolFailureCapability(),
        AskCapability(),
        TodoCapability(
            id="todos",
            description="Persisted task list for planning and tracking multi-step work.",
            defer_loading=True,
        ),
        HistoryCapability(
            octomate.thread_manager,
            id="history",
            description="Search and page every thread the person you are answering "
            "has spoken in, on any of their linked accounts.",
            defer_loading=True,
        ),
    ]
    # An MCP server answers with whatever it answers with, and a tool return persists
    # in history, so one oversized reply is re-sent on every later request for the rest
    # of the conversation. Spill and summarize each fall back to truncation, which is
    # the floor: a reduction that cannot run must not leave the payload whole.
    if config.tool_output.enabled:
        bands: list[Band] = []
        for band in config.tool_output.bands:
            match band.action:
                case TruncateAction(strategy=strategy, max_chars=max_chars):
                    action = Truncate(strategy=strategy, max_chars=max_chars)
                case SpillAction(preview_chars=preview_chars):
                    action = Spill(preview_chars=preview_chars, then=Truncate())
                case SummarizeAction():
                    action = Summarize(then=Truncate())
            bands.append(Band(over=band.over, action=action))
        capabilities.append(
            ToolOutputLimits(
                bands=bands,
                over_tokens=config.tool_output.over_tokens,
                # Spills go to the database, not local disk, so a handle read back a
                # turn later resolves in whichever process picks that turn up.
                store=SpillStore(retention=config.tool_output.retention),
            )
        )

    return InklingTentacle(
        id,
        octomate,
        models={model.name: registry.build_model(model) for model in config.models},
        claims=config.claims,
        permission_mode=config.permission_mode,
        request_limit=config.request_limit,
        gateway=config.gateway,
        capabilities=capabilities,
        system_prompt=SYSTEM_PROMPT,
    )
