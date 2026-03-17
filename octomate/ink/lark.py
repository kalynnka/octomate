from __future__ import annotations

import io
import logging
from functools import partial

import anyio.to_thread
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from pydantic import SecretStr

from octomate.tentacles.base import Mask

logger = logging.getLogger(__name__)


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

    def inspect(self) -> Mask:
        try:
            secret = self.app_secret.get_secret_value()
            resp = self.sync_http.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": secret},
            )
            resp.raise_for_status()
            token = resp.json().get("tenant_access_token")
            if not token:
                return Mask(id="", name="")

            resp = self.sync_http.get(
                "/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            bot = resp.json().get("bot", {})
            if bot:
                return Mask(
                    id=bot.get("open_id", ""),
                    name=bot.get("app_name", ""),
                )
        except Exception:
            logger.warning("LarkInk: inspect failed", exc_info=True)
        return Mask(id="", name="")

    async def upload_image(self, data: bytes) -> str | None:
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
        resp = await anyio.to_thread.run_sync(
            partial(self.client.im.v1.image.create, request),  # type: ignore[union-attr]
        )
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
    ) -> bool:
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
        resp = await anyio.to_thread.run_sync(
            partial(self.client.im.v1.message.create, request),  # type: ignore[union-attr]
        )
        if not resp.success():
            logger.warning("LarkInk: send_message failed: %s %s", resp.code, resp.msg)
        return resp.success()

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
    ) -> bool:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(msg_type)
                .build()
            )
            .build()
        )
        resp = await anyio.to_thread.run_sync(
            partial(self.client.im.v1.message.reply, request),  # type: ignore[union-attr]
        )
        if not resp.success():
            logger.warning("LarkInk: reply_message failed: %s %s", resp.code, resp.msg)
        return resp.success()

    async def download_image(
        self, message_id: str, file_key: str
    ) -> tuple[bytes, str] | None:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("image")
            .build()
        )
        resp = await anyio.to_thread.run_sync(
            partial(self.client.im.v1.message_resource.get, request),  # type: ignore[union-attr]
        )
        if not resp.success():
            logger.warning("LarkInk: download_image failed: %s %s", resp.code, resp.msg)
            return None
        file_name: str = resp.file_name or file_key
        data = resp.file.read() if resp.file else b""
        return (data, file_name)
