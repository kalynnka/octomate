from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

from pixivpy_async import AppPixivAPI, PixivClient
from pydantic import BaseModel, SecretStr
from pydantic_ai import RunContext
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.agents.manager import SkillManager

P = ParamSpec("P")
R = TypeVar("R")

TOKEN_REFRESH_INTERVAL = 15 * 60

logger = logging.getLogger(__name__)


class PixivConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIXIV_",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
        yaml_config_section="pixiv",
    )

    refresh_token: SecretStr = SecretStr("")
    search_limit: int = 8
    ranking_limit: int = 8
    image_dir: Path = Path("./pixiv/images")

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )


class PixivImageUrls(BaseModel):
    large: str | None = None
    medium: str | None = None
    square_medium: str | None = None


class PixivIllust(BaseModel):
    id: int
    title: str
    author: str
    author_id: int
    tags: list[str]
    image_urls: PixivImageUrls
    local_path: str | None = None


class PixivSearchResult(BaseModel):
    query: str
    illusts: list[PixivIllust]


class PixivTokenManager:
    client: PixivClient | None
    api: AppPixivAPI
    _last_login: float
    _lock: asyncio.Lock

    def __init__(self) -> None:
        self.client = None
        self.api = AppPixivAPI()
        self._last_login = 0
        self._lock = asyncio.Lock()

    async def ensure_auth(self, refresh_token: str) -> None:
        async with self._lock:
            if time.time() - self._last_login > TOKEN_REFRESH_INTERVAL:
                await self._login(refresh_token)

    async def _login(self, refresh_token: str) -> None:
        logger.info("Refreshing Pixiv token...")
        if self.client is None:
            self.client = PixivClient()
            self.api.session = self.client.start()
        await self.api.login(refresh_token=refresh_token)
        self._last_login = time.time()
        logger.info("Pixiv token refreshed successfully")

    async def refresh_and_retry(self, refresh_token: str) -> None:
        async with self._lock:
            self._last_login = 0
            await self._login(refresh_token)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None


token_manager = PixivTokenManager()


async def download_image(config: PixivConfig, image_url: str) -> str | None:
    cache_path = config.image_dir / image_url.split("/")[-1]
    if cache_path.exists():
        return str(cache_path)

    config.image_dir.mkdir(parents=True, exist_ok=True)
    try:
        await token_manager.api.download(
            image_url,
            path=str(config.image_dir),
            name=cache_path.name,
        )
        logger.info("Downloaded image: %s", cache_path)
        return str(cache_path)
    except Exception as e:
        logger.warning("Failed to download image %s: %s", image_url, e)
        return None


def parse_illusts(
    raw_illusts: list, local_paths: list[str | None]
) -> list[PixivIllust]:
    result: list[PixivIllust] = []
    for illust, local_path in zip(raw_illusts, local_paths):
        result.append(
            PixivIllust(
                id=illust.id,
                title=illust.title,
                author=illust.user.name,
                author_id=illust.user.id,
                tags=[tag.name for tag in illust.tags[:5]],
                image_urls=PixivImageUrls(
                    large=illust.image_urls.large,
                    medium=illust.image_urls.medium,
                    square_medium=illust.image_urls.square_medium,
                ),
                local_path=local_path,
            )
        )
    return result


def register(manager: SkillManager) -> None:
    config = PixivConfig()
    ts = manager.register(
        name="pixiv",
        description=(
            "Search and browse illustrations on Pixiv, the largest anime & manga "
            "art community. Search by keyword/tag to find artwork, or fetch daily/"
            "weekly/monthly popularity rankings. Returns illustration metadata "
            "(title, author, tags) and downloads images locally for sharing."
        ),
    )
    token = config.refresh_token.get_secret_value()

    def with_auto_refresh(
        func: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            await token_manager.ensure_auth(token)
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.warning("Pixiv API call failed, retrying after refresh: %s", e)
                await token_manager.refresh_and_retry(token)
                return await func(*args, **kwargs)

        return wrapper

    @ts.tool
    @with_auto_refresh
    async def search_illustrations(
        ctx: RunContext[Any],
        keyword: str,
        limit: int = 3,
    ) -> PixivSearchResult:
        """Search for illustrations on Pixiv by keyword or tag.

        Use this tool when the user asks for anime illustrations, fan art,
        artwork by a specific artist, or images matching a tag/theme.

        Do NOT use this tool for:
        - Real photographs or stock photos (Pixiv is illustration-focused)
        - Searching for manga/novel text content
        - Non-visual content requests
        - When the user already has an image and wants analysis

        Args:
            ctx: Run context.
            keyword: Search keyword or tag. Supports both Japanese and English.
                Examples: "landscape", "初音ミク", "original".
            limit: Maximum number of illustrations to return (default 3, max 8).

        Returns:
            A PixivSearchResult containing illustration metadata and local
            file paths for downloaded images.
        """
        result = await token_manager.api.search_illust(
            keyword, search_target="partial_match_for_tags"
        )
        if not result.illusts:
            return PixivSearchResult(query=keyword, illusts=[])

        illusts = result.illusts[: min(limit, config.search_limit)]
        urls = [i.image_urls.medium for i in illusts]
        paths = await asyncio.gather(*[download_image(config, u) for u in urls])
        return PixivSearchResult(query=keyword, illusts=parse_illusts(illusts, paths))

    @ts.tool
    @with_auto_refresh
    async def daily_ranking(
        ctx: RunContext[Any],
        mode: Literal[
            "day", "week", "month", "day_male", "day_female", "day_r18"
        ] = "day",
        limit: int = 5,
    ) -> PixivSearchResult:
        """Fetch Pixiv illustration popularity rankings.

        Use this tool when the user asks for trending, popular, or top-ranked
        illustrations on Pixiv — e.g. "show me today's best artwork" or
        "what's popular on Pixiv this week".

        Do NOT use this tool for:
        - Searching for a specific keyword or artist (use search_illustrations)
        - Non-illustration content
        - Historical rankings from a specific past date

        Args:
            ctx: Run context.
            mode: Ranking period and category.
                "day" — Daily overall ranking.
                "week" — Weekly overall ranking.
                "month" — Monthly overall ranking.
                "day_male" — Daily ranking popular with male users.
                "day_female" — Daily ranking popular with female users.
                "day_r18" — Daily R-18 ranking (adult content).
            limit: Maximum number of illustrations to return (default 5, max 8).

        Returns:
            A PixivSearchResult containing ranked illustration metadata and
            local file paths for downloaded images.
        """
        mode_names = {
            "day": "Daily Ranking",
            "week": "Weekly Ranking",
            "month": "Monthly Ranking",
            "day_male": "Daily Ranking (Male)",
            "day_female": "Daily Ranking (Female)",
            "day_r18": "Daily R-18 Ranking",
        }
        result = await token_manager.api.illust_ranking(mode)
        if not result.illusts:
            return PixivSearchResult(query=mode_names.get(mode, mode), illusts=[])

        illusts = result.illusts[: min(limit, config.ranking_limit)]
        urls = [i.image_urls.medium for i in illusts]
        paths = await asyncio.gather(*[download_image(config, u) for u in urls])
        return PixivSearchResult(
            query=mode_names.get(mode, mode),
            illusts=parse_illusts(illusts, paths),
        )
