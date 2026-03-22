from __future__ import annotations

import asyncio
import logging
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets")
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"zep_cloud")

import octotools
from octomate.agents.manager import SkillManager
from octomate.config import OctomateConfig
from octomate.octopus import Octopus

logging.basicConfig(level=logging.INFO)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


def _start() -> None:
    config = OctomateConfig()

    skill_manager = SkillManager()
    octotools.github.register(skill_manager)

    octopus = Octopus(
        config.mind, memory=config.build_memory(), skill_manager=skill_manager
    )
    config.connect_tentacles(octopus)

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
