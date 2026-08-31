from __future__ import annotations

import logging

import logfire
from fastapi import FastAPI

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.database import engine as db_engine
from octomate.managers.project import ProjectManager
from octomate.managers.user import UserManager
from octomate.managers.workspaces import MirrorManager, WorkspaceManager
from octomate.providers import ProviderHttpLogFilter, ProviderRegistry
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.deepseek import DeepseekTentacle
from octomate.tentacles.agents.inkling import build_inkling
from octomate.tentacles.base import TentacleLogFormatter
from octomate.tentacles.channels import build_channel

config = OctomateConfig()


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
        config=config,
        users=UserManager(config.users),
        workspaces=WorkspaceManager(
            projects=ProjectManager(config.projects),
            mirrors=MirrorManager(config=config.mirrors),
            config=config.workspaces,
        ),
        oauth_encryption_key=config.oauth.encryption_key,
        mcp_path=config.mcp_path,
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

    if (inkling_config := config.agents.inkling) is not None and inkling_config.enabled:
        octomate.connect(
            build_inkling(
                "inkling",
                inkling_config,
                octomate,
                registry=registry,
                mcp=config.mcp,
                integrations=config.integrations,
            )
        )

    if (claude_config := config.agents.claude) is not None and claude_config.enabled:
        octomate.connect(
            ClaudeCodeTentacle(
                "claude",
                octomate,
                config=claude_config,
            )
        )

    if (codex_config := config.agents.codex) is not None and codex_config.enabled:
        octomate.connect(
            CodexTentacle(
                "codex",
                octomate,
                config=codex_config,
            )
        )

    if (
        deepseek_config := config.agents.deepseek
    ) is not None and deepseek_config.enabled:
        octomate.connect(
            DeepseekTentacle(
                "deepseek",
                octomate,
                config=deepseek_config,
            )
        )

    for channel_id, channel_config in config.channels.items():
        if channel_config.enabled:
            octomate.connect(
                build_channel(channel_id, channel_config, octomate),
            )

    return octomate.app(title="Octomate")
