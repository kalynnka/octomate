from __future__ import annotations

import io
import logging
from typing import Any

import httpx
import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from pydantic import Field, SecretStr, model_validator

from octomate.schemas.session import UserProfile

logger = logging.getLogger(__name__)


class LarkUserProfile(UserProfile):
    user_id: str = Field(default="", validation_alias="open_id")
    title: str | None = Field(default=None, validation_alias="job_title")

    union_id: str = ""
    en_name: str = ""
    city: str = ""
    country: str = ""
    email: str = ""
    enterprise_email: str = ""
    mobile: str = ""
    employee_no: str = ""
    employee_type: int | None = None
    description: str = ""
    work_station: str = ""
    department_ids: list[str] = Field(default_factory=list)
    avatar_url: str = ""
    is_tenant_manager: bool = False
    join_time: int = 0
    time_zone: str = ""
    leader_user_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            avatar = data.pop("avatar", None)
            if avatar and hasattr(avatar, "avatar_origin") and avatar.avatar_origin:
                data.setdefault("avatar_url", avatar.avatar_origin)
            gender = data.get("gender")
            if gender:
                data["gender"] = {1: "male", 2: "female", 3: "other"}.get(gender)
            if not data.get("name"):
                data["name"] = data.get("nickname") or data.get("en_name") or ""
        return data


class LarkInk:
    app_id: str
    app_secret: SecretStr
    client: lark.Client
    sync_http: httpx.Client

    def __init__(self, app_id: str, app_secret: SecretStr) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret.get_secret_value())
            .build()
        )
        self.sync_http = httpx.Client(base_url="https://open.feishu.cn")

        # Authenticate immediately to verify credentials and populate token for subsequent calls
        secret = self.app_secret.get_secret_value()
        resp = self.sync_http.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": secret},
        )
        resp.raise_for_status()
        token = resp.json().get("tenant_access_token")
        if not token:
            raise RuntimeError("LarkInk: failed to obtain tenant_access_token")
        self.sync_http.headers["Authorization"] = f"Bearer {token}"

    def inspect(self) -> LarkUserProfile:
        resp = self.sync_http.get("/open-apis/bot/v3/info")
        resp.raise_for_status()
        bot = resp.json().get("bot", {})
        if bot:
            return LarkUserProfile(
                user_id=bot.get("open_id", ""),
                name=bot.get("app_name", ""),
                avatar_url=bot.get("avatar_url", ""),
            )
        raise RuntimeError("LarkInk: inspect failed, no bot info returned")

    async def upload_media(self, data: bytes) -> str | None:
        request = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(io.BytesIO(data))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.image.acreate(request)  # type: ignore[union-attr]
        if resp.success() and resp.data and resp.data.image_key:
            return resp.data.image_key
        logger.warning("LarkInk: image upload failed: %s %s", resp.code, resp.msg)
        return None

    async def send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
    ) -> str | None:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(request)  # type: ignore[union-attr]
        if not resp.success():
            logger.warning("LarkInk: send_message failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        body = (
            ReplyMessageRequestBody.builder()
            .content(content)
            .msg_type(msg_type)
            .reply_in_thread(reply_in_thread)
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = await self.client.im.v1.message.areply(request)  # type: ignore[union-attr]
        if not resp.success():
            logger.warning("LarkInk: reply_message failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    async def get_user_profile(self, user_id: str) -> LarkUserProfile:
        try:
            request = (
                GetUserRequest.builder()
                .user_id_type("open_id")
                .user_id(user_id)
                .build()
            )
            resp = await self.client.contact.v3.user.aget(request)  # type: ignore[union-attr]
            if resp.success() and resp.data and resp.data.user:
                attrs = {k: v for k, v in vars(resp.data.user).items() if v is not None}
                profile = LarkUserProfile.model_validate(attrs)
                if profile.name:
                    return profile
                logger.warning(
                    "LarkInk: get_user_profile returned no name for %s "
                    "(fields: %s). Grant 'name' field permission in the "
                    "Feishu developer portal → Permissions → Contact fields.",
                    user_id,
                    list(attrs.keys()),
                )
            else:
                logger.warning(
                    "LarkInk: get_user_profile failed (code=%s, msg=%s). "
                    "Ensure the app has 'contact:user.base:readonly' scope.",
                    resp.code,
                    resp.msg,
                )
        except Exception:
            logger.warning(
                "LarkInk: get_user_profile failed for %s", user_id, exc_info=True
            )
        return LarkUserProfile(user_id=user_id, name=user_id)

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        file_key = kwargs.get("file_key", resource_id)
        request = (
            GetMessageResourceRequest.builder()
            .message_id(resource_id)
            .file_key(file_key)
            .type("image")
            .build()
        )
        resp = await self.client.im.v1.message_resource.aget(request)  # type: ignore[union-attr]
        if not resp.success():
            logger.warning("LarkInk: download_media failed: %s %s", resp.code, resp.msg)
            return None
        file_name: str = resp.file_name or file_key
        data = resp.file.read() if resp.file else b""
        return (data, file_name)
