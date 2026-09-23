"""NapCat OneBot HTTP API client."""

from __future__ import annotations

import logging

import httpx
from pydantic import JsonValue, SecretStr

from octomate.schemas.segments import ImageSegment
from octomate.tentacles.channel import DownloadedImage, Ink
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.napcat.schema import (
    ActionResponse,
    ImageInfo,
    NapcatOutboundMessage,
    NapcatUserProfile,
    SentMessage,
)
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)


class NapcatInk(Ink[NapcatOutboundMessage]):
    """OneBot HTTP transport: private and group sends, stranger lookups and image
    fetches."""

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

    async def call_api[DataT](
        self,
        endpoint: str,
        payload: JsonObject,
        response_model: type[ActionResponse[DataT]],
    ) -> DataT:
        resp = await self.httpx.post(endpoint, json=payload)
        resp.raise_for_status()
        result = response_model.model_validate_json(resp.content)
        if result.status != "ok" or result.retcode != 0:
            raise RuntimeError(
                f"NapCat {endpoint} failed ({result.retcode}): "
                f"{result.message or result.wording or result.status}"
            )
        if result.data is None:
            raise ValueError(f"NapCat {endpoint} returned no data")
        return result.data

    async def inspect(self) -> NapcatUserProfile:
        return await self.call_api(
            "/get_login_info", {}, ActionResponse[NapcatUserProfile]
        )

    async def get_user_profile(self, user_id: str) -> NapcatUserProfile:
        try:
            data = await self.call_api(
                "/get_stranger_info",
                {"user_id": user_id},
                ActionResponse[JsonObject],
            )
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
        data = await self.call_api(
            "/get_image", {"file": file}, ActionResponse[ImageInfo]
        )
        return data.url

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
        if chat_type not in {"dm", "group"}:
            raise ValueError("NapCat supports only group chats and DMs")
        first_msg_id: IMMessageID | None = None
        endpoint = "/send_private_msg" if chat_type == "dm" else "/send_group_msg"
        id_field = "user_id" if chat_type == "dm" else "group_id"
        for message in messages:
            segments: list[JsonValue] = [*message.segments]
            if reply_to:
                segments.insert(0, {"type": "reply", "data": {"id": reply_to}})
            payload: JsonObject = {
                id_field: chat_id,
                "message": segments,
            }
            data = await self.call_api(endpoint, payload, ActionResponse[SentMessage])
            if data.message_id is None:
                raise ValueError(f"NapCat {endpoint} returned no message_id")
            first_msg_id = first_msg_id or data.message_id
        return first_msg_id

    async def close(self) -> None:
        await self.httpx.aclose()
