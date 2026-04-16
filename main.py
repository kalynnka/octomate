from __future__ import annotations

import asyncio
import logging
import warnings

import octotools

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"zep_cloud")

from octomate.config import (
    ClaudeCodeConfig,
    CopilotConfig,
    DeepResearchConfig,
    FastResearchConfig,
    LarkTentacleConfig,
    NapcatTentacleConfig,
    OctomateConfig,
    SlackTentacleConfig,
)
from octomate.memory import Mem0Memory, OctopusMemory, ZepMemory
from octomate.octopus import Octopus
from octomate.tentacles.agent.pulse import PulseTentacle
from octomate.tentacles.agent.skills import SkillLibrary, SkillManager

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _build_skill_library(config: OctomateConfig) -> SkillLibrary | None:
    roots = config.tentacles[0].pulse.skill_roots
    library = SkillLibrary(roots)
    library.discover()
    return library if library.docs else None


def _build_octopus() -> Octopus:
    config = OctomateConfig()

    skill_library = _build_skill_library(config)

    skill_manager = SkillManager()
    octotools.streamify.register(skill_manager)
    octotools.github.register(skill_manager)
    octotools.linear.register(skill_manager)
    octotools.tarot.register(skill_manager)
    if skill_library:
        skill_manager.register_skill_library("skills", skill_library)

    napcat_skill_manager = SkillManager()
    octotools.streamify.register(napcat_skill_manager)
    octotools.tarot.register(napcat_skill_manager)
    if skill_library:
        napcat_skill_manager.register_skill_library("skills", skill_library)

    octopus = Octopus(skill_manager=skill_manager)

    for ac in config.agents:
        if isinstance(ac, ClaudeCodeConfig):
            from octomate.tentacles.agent.claude import ClaudeCodeTentacle

            octopus.graft(ClaudeCodeTentacle(ac.tag, octopus, ac))
        elif isinstance(ac, CopilotConfig):
            from octomate.tentacles.agent.copilot import CopilotTentacle

            octopus.graft(CopilotTentacle(ac.tag, octopus, ac))
        elif isinstance(ac, FastResearchConfig):
            from octomate.tentacles.agent.research import FastResearchTentacle

            octopus.graft(FastResearchTentacle(ac.tag, octopus, ac))
        elif isinstance(ac, DeepResearchConfig):
            from octomate.tentacles.agent.research import DeepResearchTentacle

            octopus.graft(DeepResearchTentacle(ac.tag, octopus, ac))

    for tc in config.tentacles:
        mem = tc.memory
        if mem.mem0.enabled:
            memory = Mem0Memory(config=mem.mem0)
        elif mem.zep.enabled:
            memory = ZepMemory(api_key=mem.zep.api_key)
        else:
            memory = OctopusMemory()

        if isinstance(tc, NapcatTentacleConfig):
            from octomate.tentacles.channel.napcat import NapcatTentacle

            octopus.connect(
                NapcatTentacle(
                    tc.name,
                    octopus,
                    ws_url=tc.ws_url,
                    http_url=str(tc.http_url),
                    access_token=tc.access_token,
                    backoff_base=tc.backoff_base,
                    backoff_max=tc.backoff_max,
                    backoff_factor=tc.backoff_factor,
                    pulse=PulseTentacle(octopus, tc.pulse, napcat_skill_manager),
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )
        elif isinstance(tc, LarkTentacleConfig):
            from octomate.tentacles.channel.lark import LarkTentacle

            warnings.filterwarnings(
                "ignore", category=DeprecationWarning, module=r"lark_oapi"
            )
            octopus.connect(
                LarkTentacle(
                    tc.name,
                    octopus,
                    app_id=tc.app_id,
                    app_secret=tc.app_secret,
                    pulse=PulseTentacle(octopus, tc.pulse, skill_manager),
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )
        elif isinstance(tc, SlackTentacleConfig):
            from octomate.tentacles.channel.slack import SlackTentacle

            octopus.connect(
                SlackTentacle(
                    tc.name,
                    octopus,
                    bot_token=tc.bot_token,
                    app_token=tc.app_token,
                    pulse=PulseTentacle(octopus, tc.pulse, skill_manager),
                    memory=memory,
                    flush_delay=tc.flush_delay,
                )
            )

    return octopus


async def app(scope, receive, send):
    if scope["type"] != "lifespan":
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                octopus = _build_octopus()
                asyncio.create_task(octopus.activate())
                await send({"type": "lifespan.startup.complete"})
            except Exception as e:
                logging.error(f"Error during startup: {e}")
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
