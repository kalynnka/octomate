from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

logger = logging.getLogger(__name__)


class NapcatMask(BaseModel):
    model_config = ConfigDict(
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
        coerce_numbers_to_str=True,
    )

    id: str = Field(default="", alias="user_id")
    name: str = Field(default="", alias="nickname")
    uid: str = ""
    qid: str = ""
    uin: str = ""
    nick: str = ""
    long_nick: str = ""
    sex: str = "unknown"
    age: int = 0
    qq_level: int = 0
    login_days: int = 0
    reg_time: int = 0
    is_vip: bool = False
    is_years_vip: bool = False
    vip_level: int = 0


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

    def inspect(self) -> NapcatMask:
        try:
            resp = self.sync_httpx.post("/get_login_info", json={})
            resp.raise_for_status()
            login_data = resp.json().get("data")

            resp = self.sync_httpx.post(
                "/get_stranger_info",
                json={"user_id": login_data["user_id"]},
            )
            resp.raise_for_status()
            return NapcatMask.model_validate(resp.json().get("data", {}))
        except Exception:
            logger.warning("NapcatInk: inspect failed", exc_info=True)
            raise

    async def get_image_url(self, file: str) -> str | None:
        resp = await self.httpx.post("/get_image", json={"file": file})
        resp.raise_for_status()
        return resp.json().get("data", {}).get("url")

    async def download(self, url: str) -> httpx.Response:
        resp = await self.httpx.get(url)
        resp.raise_for_status()
        return resp
