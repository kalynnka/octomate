from __future__ import annotations

import logging

import logfire
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_ai import AgentCapability
from pydantic_ai_harness.tool_output_limits import (
    Band,
    Spill,
    Summarize,
    ToolOutputLimits,
    Truncate,
)
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

from octomate import Octomate
from octomate.capabilities.ask import AskCapability
from octomate.capabilities.history import HistoryCapability
from octomate.capabilities.todos import TodoCapability
from octomate.capabilities.tools import ToolFailureCapability
from octomate.config import OctomateConfig
from octomate.config.agents import SpillAction, SummarizeAction, TruncateAction
from octomate.database import engine as db_engine
from octomate.integrations import build_integration
from octomate.managers.project import ProjectManager
from octomate.managers.spills import SpillStore
from octomate.managers.user import UserManager
from octomate.providers import ProviderHttpLogFilter, ProviderRegistry
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.inkling import InklingTentacle, build_mcp_toolsets
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.base import TentacleLogFormatter
from octomate.tentacles.channels.lark import LarkTentacle
from octomate.tentacles.channels.napcat import NapcatTentacle
from octomate.tentacles.channels.slack import SlackTentacle
from octomate.tentacles.channels.web.trunkline import TrunklineTentacle

config = OctomateConfig()


def hook_secret(agent: str) -> SecretStr:
    """The credential `agent`'s hook router authenticates against.

    Demanded rather than defaulted: these routers write a session's prompts and answers
    into thread history, which agents read back, so serving one without a credential
    would let anything that can reach the port speak as the human. Refusing to boot is
    the only honest answer — the alternative is an open router nobody notices.

    Asked for only where a hook-serving tentacle is actually being built, so a
    deployment that configures neither Claude nor Codex needs no secret at all.
    """
    if config.hook_secret is None:
        raise RuntimeError(
            f"octomate.hook_secret is unset, but agents.{agent} serves a hook router "
            "that authenticates against it. Run `octomate hooks secret` to generate one "
            f"and place it, then re-run `octomate {agent} hooks install`."
        )
    return config.hook_secret


def health_probes_are_noise(record: logging.LogRecord) -> bool:
    """Drop access lines for health polls — the console asks every 15s and a
    200 says nothing. Sits on the `uvicorn.access` logger, so console and
    Logfire both skip them; a failing probe still surfaces through the
    console's own offline chip and Logfire's traces."""
    args = record.args
    # uvicorn.access args: (client_addr, method, path, http_version, status).
    return not (
        isinstance(args, tuple) and len(args) == 5 and str(args[2]).endswith("/health")
    )


def create_app() -> FastAPI:
    """Build the Octomate FastAPI app.

    All setup lives here, not at import time, so that uvicorn's reload supervisor
    and worker processes can import this module without re-running logfire
    instrumentation, channel auth, and the rest. Only the process that actually
    serves calls the factory (`uvicorn main:create_app --factory`).
    """
    logfire.configure(
        service_name=config.logfire.service_name,
        environment=config.logfire.environment,
        send_to_logfire="if-token-present" if config.logfire.send_to_logfire else False,
        console=logfire.ConsoleOptions() if config.logfire.console else False,
        scrubbing=logfire.ScrubbingOptions(
            extra_patterns=[
                "conversation_address",
                "source_address",
                "target_address",
                "user_id",
                "chat_id",
                "responder_id",
                "message_id",
            ]
        )
        if config.logfire.scrub
        else False,
    )
    # Library auto-instrumentation is opt-in per library (diagnostic-session volume);
    # Octomate's own octomate.*-scoped spans are not behind these flags.
    if config.logfire.instrument.pydantic_ai:
        logfire.instrument_pydantic_ai()
    if config.logfire.instrument.httpx:
        logfire.instrument_httpx()
    if config.logfire.instrument.sqlalchemy:
        logfire.instrument_sqlalchemy(engine=db_engine())

    octomate = Octomate(
        users=UserManager(config.users),
        projects=ProjectManager(config.projects),
        oauth_encryption_key=config.oauth.encryption_key,
    )

    inkling_capabilities: list[AgentCapability[None]] = [
        # Observational only: it warns, it never edits a request. A collapsed prompt
        # cache is otherwise invisible — the run still succeeds, just slower and dearer.
        WarnOnCacheBusts(),
        ToolFailureCapability(),
        AskCapability(),
        TodoCapability(
            id="todos",
            description="Persisted task list for planning and tracking "
            "multi-step work.",
            defer_loading=True,
        ),
        HistoryCapability(
            octomate.conversations,
            octomate.thread_manager,
            id="history",
            description="Search and page this thread's chat ledger and "
            "this conversation's model ledger.",
            defer_loading=True,
        ),
    ]
    # One capability per configured integration; each run mounts its own copy of one,
    # bound to the user that run is answering.
    inkling_capabilities.extend(
        build_integration(name, integration, octomate.oauth)
        for name, integration in config.integrations.items()
        if integration.enabled
    )

    # An MCP server answers with whatever it answers with, and a tool return persists
    # in history, so one oversized reply is re-sent on every later request for the rest
    # of the conversation. Spill and summarize each fall back to truncation, which is
    # the floor: a reduction that cannot run must not leave the payload whole.
    tool_output = config.agents.inkling.tool_output
    if tool_output.enabled:
        bands: list[Band] = []
        for band in tool_output.bands:
            match band.action:
                case TruncateAction(strategy=strategy, max_chars=max_chars):
                    action = Truncate(strategy=strategy, max_chars=max_chars)
                case SpillAction(preview_chars=preview_chars):
                    action = Spill(preview_chars=preview_chars, then=Truncate())
                case SummarizeAction():
                    action = Summarize(then=Truncate())
            bands.append(Band(over=band.over, action=action))
        inkling_capabilities.append(
            ToolOutputLimits(
                bands=bands,
                over_tokens=tool_output.over_tokens,
                # Spills go to the database, not local disk, so a handle read back a
                # turn later resolves in whichever process picks that turn up.
                store=SpillStore(retention=tool_output.retention),
            )
        )

    console_handler = logging.StreamHandler()
    # Tint the level + each tentacle's header, but only on a real terminal so the
    # ANSI codes don't leak into piped/redirected logs.
    console_handler.setFormatter(
        TentacleLogFormatter(octomate, colorize=console_handler.stream.isatty())
    )
    logging.basicConfig(
        level=config.logging.level,
        handlers=[console_handler, logfire.LogfireLoggingHandler()],
        force=True,
    )
    # Uvicorn wired its own handlers before importing this factory, which is why
    # its lines arrive bare ("INFO: ...") and never reach Logfire. Route its
    # loggers through the root pipeline instead: one format, one Logfire sink.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").addFilter(health_probes_are_noise)
    # The inkling agent's cache-bust monitor reports through `warnings`, which otherwise
    # writes straight to stderr and never reaches Logfire — where a cache collapse is
    # only visible as a cost and latency regression nobody attributes to a busted prefix.
    logging.captureWarnings(True)
    registry = ProviderRegistry(config.providers)
    # httpx logs every request at INFO; keep the LLM-provider round-trips and drop
    # the rest (Lark cardkit streaming PUTs especially). Logfire still traces every
    # request regardless. Per-logger overrides from config win.
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.addFilter(ProviderHttpLogFilter(registry))
    # watchfiles logs a line per change detected, and the Claude transcript tailer keeps
    # a watch on a directory its client rewrites through every turn — a stream of "1
    # change detected" saying only that a file moved. What came of the change is the
    # session's own story, which the hook and tailer lines tell.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    for name, level in config.logging.loggers.items():
        logging.getLogger(name).setLevel(level)

    octomate.connect(
        InklingTentacle(
            "inkling",
            octomate,
            models={
                model.name: registry.build_model(model)
                for model in config.agents.inkling.models
            },
            claims=config.agents.inkling.claims,
            permission_mode=config.agents.inkling.permission_mode,
            toolsets=build_mcp_toolsets(config.mcp),
            capabilities=inkling_capabilities,
            system_prompt=SYSTEM_PROMPT,
        ),
    )

    if (claude_config := config.agents.claude) is not None:
        octomate.connect(
            ClaudeCodeTentacle(
                "claude",
                octomate,
                config=claude_config,
                hook_secret=hook_secret("claude"),
            )
        )

    if (codex_config := config.agents.codex) is not None and codex_config.enabled:
        octomate.connect(
            CodexTentacle(
                "codex",
                octomate,
                config=codex_config,
                hook_secret=hook_secret("codex"),
            )
        )

    if (channel_config := config.channels.slack) is not None and channel_config.enabled:
        octomate.connect(
            SlackTentacle(
                "slack",
                octomate,
                config=channel_config,
            )
        )

    if (channel_config := config.channels.lark) is not None and channel_config.enabled:
        octomate.connect(
            LarkTentacle(
                "lark",
                octomate,
                config=channel_config,
            )
        )

    if (
        channel_config := config.channels.napcat
    ) is not None and channel_config.enabled:
        octomate.connect(
            NapcatTentacle(
                "napcat",
                octomate,
                config=channel_config,
            )
        )

    if (
        channel_config := config.channels.trunkline
    ) is not None and channel_config.enabled:
        octomate.connect(
            TrunklineTentacle(
                "trunkline",
                octomate,
                config=channel_config,
            )
        )

    return octomate.app(title="Octomate")
