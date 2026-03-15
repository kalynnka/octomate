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
from octomate.nerve import OctopusNerve
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

    octopus = Octopus(
        OctopusNerve(),
        config.mind,
        skill_manager=skill_manager,
    )
    for tc in config.tentacles:
        if isinstance(tc, NapcatTentacleConfig):
            octopus.connect(NapcatTentacle(tc, flush_delay=config.mind.flush_delay))
        elif isinstance(tc, LarkTentacleConfig):
            octopus.connect(LarkTentacle(tc, flush_delay=config.mind.flush_delay))
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
