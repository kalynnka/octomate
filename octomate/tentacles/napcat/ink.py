from __future__ import annotations

import logging

import httpx
from pydantic import JsonValue, SecretStr

from octomate.schemas.segments import ImageSegment
from octomate.tentacles.channel import DownloadedImage, Ink
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.napcat.schema import (
    NapcatOutboundMessage,
    NapcatUserProfile,
)
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)


class NapcatInk(Ink[NapcatOutboundMessage]):
    http_url: str
    access_token: SecretStr | None
    httpx: httpx.AsyncClient

    def __init__(self, http_url: str, access_token: SecretStr | None = None) -> None:
        self.http_url = str(http_url).rstrip("/")
        self.access_token = access_token
        headers: dict[str, str] = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token.get_secret_value()}"
        self.httpx = httpx.AsyncClient(base_url=self.http_url, headers=headers)

    async def inspect(self) -> NapcatUserProfile:
        resp = await self.httpx.post("/get_login_info", json={})
        resp.raise_for_status()
        login_data = resp.json().get("data", {})
        user_id = str(login_data.get("user_id", ""))
        resp = await self.httpx.post(
            "/get_stranger_info",
            json={"user_id": user_id},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        data.setdefault("user_id", user_id)
        return NapcatUserProfile.model_validate(data)

    async def get_user_profile(self, user_id: str) -> NapcatUserProfile:
        try:
            resp = await self.httpx.post(
                "/get_stranger_info",
                json={"user_id": user_id},
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            data.setdefault("user_id", user_id)
            return NapcatUserProfile.model_validate(data)
        except Exception:
            logger.warning(
                "NapcatInk: get_user_profile failed for %s", user_id, exc_info=True
            )
            return NapcatUserProfile(channel_user_id=user_id, name=user_id)

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def get_image_url(self, file: str) -> str | None:
        resp = await self.httpx.post("/get_image", json={"file": file})
        resp.raise_for_status()
        return resp.json().get("data", {}).get("url")

    async def download(self, url: str) -> httpx.Response:
        resp = await self.httpx.get(url)
        resp.raise_for_status()
        return resp

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        try:
            url = seg.data.url or await self.get_image_url(str(seg.data.file))
            if not url:
                return None
            resp = await self.download(url)
            return DownloadedImage(
                data=resp.content,
                file_name=url.rsplit("/", 1)[-1] or str(seg.data.file),
                content_type=resp.headers.get("content-type", ""),
                url=url,
            )
        except Exception:
            logger.warning("NapcatInk: download_image failed", exc_info=True)
            return None

    async def open_dm(self, user_id: str, opener: str | None = None) -> str | None:
        """A QQ user's own id is their private chat id, so nothing has to be opened."""
        return user_id or None

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[NapcatOutboundMessage],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        first_msg_id: IMMessageID | None = None
        if not reply_to and chat_type == "thread":
            reply_to = channel_thread_id
        endpoint = "/send_private_msg" if chat_type == "dm" else "/send_group_msg"
        id_field = "user_id" if chat_type == "dm" else "group_id"
        for message in messages:
            segments: list[JsonValue] = [*message.segments]
            payload: JsonObject = {
                id_field: chat_id,
                "message": segments,
            }
            if reply_to:
                payload["reply"] = reply_to
            try:
                resp = await self.httpx.post(endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                first_msg_id = first_msg_id or data.get("message_id")
            except Exception:
                logger.warning("NapcatInk: send_message failed", exc_info=True)
        return first_msg_id

    async def close(self) -> None:
        await self.httpx.aclose()
