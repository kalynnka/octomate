# Tarot divination tool for pydantic-ai agents.
# Card data, formations and meanings referenced from:
# https://github.com/MinatoAquaCrews/nonebot_plugin_tarot

from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.tentacles.agent.skills import SkillManager

logger = logging.getLogger(__name__)

TAROT_DATA_PATH = Path(__file__).parent / "tarot.json"

with open(TAROT_DATA_PATH, "r", encoding="utf-8") as _f:
    _tarot_data = json.load(_f)
    FORMATIONS: dict[str, dict] = _tarot_data["formations"]
    CARDS: dict[str, dict] = _tarot_data["cards"]


class TarotConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TAROT_",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
        yaml_config_section="tarot",
    )

    image_dir: Path = Path(__file__).parent / "resources"

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )


class TarotCard(BaseModel):
    name_cn: str
    name_en: str
    card_type: str
    orientation: Literal["upright", "reversed"]
    meaning: str
    position: str
    image_path: str | None = None


class TarotSpreadResult(BaseModel):
    formation_name: str
    question: str
    cards: list[TarotCard]


def resolve_card_image(image_dir: Path, card_type: str, pic: str) -> str | None:
    card_dir = image_dir / card_type
    if not card_dir.is_dir():
        return None
    for p in card_dir.glob(pic + ".*"):
        return str(p)
    return None


def draw_spread(
    question: str,
    image_dir: Path,
    formation_name: str | None = None,
) -> TarotSpreadResult:
    seed = int.from_bytes(hashlib.sha256(question.encode()).digest()[:8])
    rng = random.Random(seed)

    if formation_name and formation_name in FORMATIONS:
        chosen_name = formation_name
    else:
        chosen_name = rng.choice(list(FORMATIONS))

    formation = FORMATIONS[chosen_name]
    cards_num: int = formation["cards_num"]
    is_cut: bool = formation["is_cut"]
    representations: list[str] = rng.choice(formation["representations"])

    drawn_keys = rng.sample(list(CARDS), cards_num)

    result_cards: list[TarotCard] = []
    for i, key in enumerate(drawn_keys):
        card_data = CARDS[key]
        upright = rng.random() < 0.5

        if is_cut and i == cards_num - 1:
            position = f"切牌「{representations[i]}」"
        else:
            position = f"第{i + 1}张牌「{representations[i]}」"

        result_cards.append(
            TarotCard(
                name_cn=card_data["name_cn"],
                name_en=card_data["name_en"],
                card_type=card_data["type"],
                orientation="upright" if upright else "reversed",
                meaning=card_data["meaning"]["up" if upright else "down"],
                position=position,
                image_path=resolve_card_image(
                    image_dir, card_data["type"], card_data["pic"]
                ),
            )
        )

    return TarotSpreadResult(
        formation_name=chosen_name,
        question=question,
        cards=result_cards,
    )


def register(manager: SkillManager) -> None:
    config = TarotConfig()
    ts = manager.register(
        name="tarot",
        description=(
            "Tarot card divination tool. Draws cards from a standard 78-card "
            "tarot deck using various spread formations (圣三角, 六芒星, etc.), "
            "determines upright/reversed orientation, and returns card meanings "
            "for the agent to interpret into a divination reading."
        ),
    )

    @ts.tool
    async def draw_tarot_spread(
        ctx: RunContext[Any],
        question: str,
        formation: str | None = None,
    ) -> TarotSpreadResult:
        """Draw tarot cards in a spread formation for divination.

        Use this tool ONLY when the user explicitly asks for tarot divination,
        fortune telling with tarot cards, or a tarot reading.

        Do NOT use this tool for:
        - General fortune telling or horoscopes unrelated to tarot
        - Any request that does not specifically mention tarot or card divination
        - Casual conversation about tarot as a topic

        The tool draws cards deterministically based on the question. You
        should interpret the drawn cards and their meanings into a coherent
        divination narrative for the user.

        When making the final divination reading for the user,
        you should explain how the position, orientation, and meaning of each card with the card picture (if available) first,
        then an overall formation and how the cards relate to each other in the formation,
        finally weave together the meanings of the drawn cards into a coherent story.

        Available formations: 圣三角牌阵, 时间之流牌阵, 四要素牌阵, 五牌阵,
        吉普赛十字阵, 马蹄牌阵, 六芒星牌阵, 平安扇牌阵, 沙迪若之星牌阵.

        Args:
            ctx: Run context.
            question: The user's question or topic for divination. This is also
                used as a seed for card selection, so the same question yields
                the same spread.
            formation: Optional formation/spread name (e.g. "六芒星牌阵").
                If omitted, a formation is randomly chosen based on the question.

        Returns:
            A TarotSpreadResult containing the formation used, each drawn card's
            name, orientation (upright/reversed), meaning, position label, and
            an optional image path.
        """
        return draw_spread(question, config.image_dir, formation)
