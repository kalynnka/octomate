from __future__ import annotations

import logging

import httpx
from pydantic import (
    AliasGenerator,
    ConfigDict,
    Field,
    SecretStr,
)
from pydantic.alias_generators import to_camel

from octomate.schemas.session import UserProfile

logger = logging.getLogger(__name__)


class NapcatUserProfile(UserProfile):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(validation_alias=to_camel),
    )

    name: str = Field(default=..., validation_alias="nick")
    nickname: str | None = Field(default=None)
    gender: str | None = Field(default=None, validation_alias="sex")

    uid: str = ""
    qid: str = ""
    uin: str = ""
    long_nick: str = ""
    qq_level: int = 0
    login_days: int = 0
    reg_time: int = 0
    is_vip: bool = False
    is_years_vip: bool = False
    vip_level: int = 0
    remark: str = ""
    constellation: int = 0
    sheng_xiao: int = 0
    blood_type: int = Field(default=0, validation_alias="kBloodType")
    home_town: str = ""
    make_friend_career: int = 0
    pos: str = ""
    college: str = ""
    country: str = ""
    province: str = ""
    city: str = ""
    post_code: str = ""
    address: str = ""
    interest: str = ""
    labels: list[str] = Field(default_factory=list)
    birthday_year: int = 0
    birthday_month: int = 0
    birthday_day: int = 0
    email: str = Field(default="", validation_alias="eMail")
    phone_num: str = ""
    category_id: int = 0
    rich_time: int = 0
    status: int = 0
    ext_status: int = 0
    battery_status: int = 0
    term_type: int = 0
    net_type: int = 0
    icon_type: int = 0
    custom_status: str | None = None
    set_time: str = "0"
    special_flag: int = 0
    abi_flag: int = 0
    e_network_type: int = 0
    show_name: str = ""
    term_desc: str = ""


class NapcatInk:
    http_url: str
    access_token: SecretStr | None
    httpx: httpx.AsyncClient
    sync_httpx: httpx.Client

    def __init__(self, http_url: str, access_token: SecretStr | None = None) -> None:
        self.http_url = str(http_url).rstrip("/")
        self.access_token = access_token
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token.get_secret_value()}"
        self.httpx = httpx.AsyncClient(base_url=self.http_url, headers=headers)
        self.sync_httpx = httpx.Client(base_url=self.http_url, headers=headers)

    def inspect(self) -> NapcatUserProfile:
        try:
            resp = self.sync_httpx.post("/get_login_info", json={})
            resp.raise_for_status()
            login_data = resp.json().get("data")

            resp = self.sync_httpx.post(
                "/get_stranger_info",
                json={"user_id": login_data["user_id"]},
            )
            resp.raise_for_status()
            return NapcatUserProfile.model_validate(resp.json().get("data", {}))
        except Exception:
            logger.warning("NapcatInk: inspect failed", exc_info=True)
            raise

    async def get_user_profile(self, user_id: str) -> NapcatUserProfile:
        try:
            resp = await self.httpx.post(
                "/get_stranger_info", json={"user_id": user_id}
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return NapcatUserProfile.model_validate(data)
        except Exception:
            logger.warning(
                "NapcatInk: get_user_profile failed for %s", user_id, exc_info=True
            )
            return NapcatUserProfile(user_id=user_id, name="unknown")

    async def get_image_url(self, file: str) -> str | None:
        resp = await self.httpx.post("/get_image", json={"file": file})
        resp.raise_for_status()
        return resp.json().get("data", {}).get("url")

    async def download(self, url: str) -> httpx.Response:
        resp = await self.httpx.get(url)
        resp.raise_for_status()
        return resp
