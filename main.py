from __future__ import annotations

import asyncio
import logging
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"lark_oapi")
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")

import octotools
from octomate.agents.manager import SkillManager
from octomate.config import LarkTentacleConfig, NapcatTentacleConfig, OctomateConfig
from octomate.memory import Mem0Memory, OctopusMemory, ZepMemory
from octomate.octopus import Octopus
from octomate.tentacles.lark import LarkTentacle
from octomate.tentacles.napcat import NapcatTentacle

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _start() -> None:
    config = OctomateConfig()

    skill_manager = SkillManager()

    octotools.qweather.register(skill_manager)
    octotools.pixiv.register(skill_manager)
    octotools.streamify.register(skill_manager)
    # octotools.github.register(skill_manager)

    mem_cfg = config.mind.memory
    if mem_cfg.mem0.enabled:
        memory: OctopusMemory = Mem0Memory(
            max_messages=mem_cfg.max_messages,
            config=mem_cfg.mem0,
        )
    elif mem_cfg.zep.enabled:
        memory = ZepMemory(
            api_key=mem_cfg.zep.api_key,
            max_messages=mem_cfg.max_messages,
        )
    else:
        memory = OctopusMemory(max_messages=mem_cfg.max_messages)

    octopus = Octopus(
        config.mind,
        memory=memory,
        skill_manager=skill_manager,
    )
    flush_delay = config.mind.flush_delay

    for tc in config.tentacles:
        if isinstance(tc, NapcatTentacleConfig):
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
                    flush_delay=flush_delay,
                )
            )
        elif isinstance(tc, LarkTentacleConfig):
            octopus.connect(
                LarkTentacle(
                    tc.name,
                    octopus,
                    app_id=tc.app_id,
                    app_secret=tc.app_secret,
                    flush_delay=flush_delay,
                )
            )
    try:
        asyncio.run(octopus.activate())
    except KeyboardInterrupt:
        pass


def run() -> None:
    if "--reload" in sys.argv:
        from watchfiles import run_process

        run_process(
            "octomate",
            target="main._start",
            target_type="function",
        )
    else:
        _start()


if __name__ == "__main__":
    run()
