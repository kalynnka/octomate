from __future__ import annotations

import io
import json
import logging
from typing import Self

import httpx
import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.core.http import Transport

# The url/header builders are module-private, but they are the request encoding
# itself — reusing them is what keeps pooled_aexecute byte-identical to the
# SDK's own transport.
from lark_oapi.core.http.transport import _build_header, _build_url
from lark_oapi.core.json import JSON
from lark_oapi.core.model import BaseRequest, Config, RawResponse, RequestOption
from pydantic import SecretStr
from uuid_utils import uuid7

from octomate.schemas.segments import ImageSegment
from octomate.telemetry import lark_logfire
from octomate.tentacles.channel import DownloadedImage, Ink
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.lark.schema import (
    LarkOutboundMessage,
    LarkStreamCard,
    LarkUserProfile,
)

logger = logging.getLogger(__name__)


SDK_AEXECUTE = Transport.aexecute

# Each entered ink's own pooled client, keyed by the `Config` its `lark.Client`
# dispatches with — the one argument `Transport.aexecute` receives that
# identifies the calling app (every generated service of a built client shares
# that exact instance).
pools: dict[Config, httpx.AsyncClient] = {}


async def pooled_aexecute(
    conf: Config,
    req: BaseRequest,
    option: RequestOption | None = None,
) -> RawResponse:
    """`Transport.aexecute` reimplemented over long-lived clients — the SDK
    leaves no polite way to pool. Its own version opens
    `async with httpx.AsyncClient()` per request: a connection-pooling library
    used as a disposable, paying a fresh TCP+TLS handshake on every card
    update. `Config` will configure anything except the client, and the
    generated `a*` methods call this staticmethod directly, so neither
    injection nor subclassing gets in. Hence the impolite way: entering a
    `LarkInk` rebinds the staticmethod to this. The patch is process-wide but
    each request routes to the calling ink's own pool via `pools`; a config no
    entered ink owns — some other `lark.Client` in the process — keeps the
    SDK's stock behavior."""
    client = pools.get(conf)
    if client is None:
        return await SDK_AEXECUTE(conf, req, option)
    if option is None:
        option = RequestOption()
    if req.http_method is None or req.uri is None:
        raise ValueError(
            f"lark request is missing http_method or uri "
            f"(method={req.http_method}, uri={req.uri})"
        )
    url = _build_url(conf.domain, req.uri, req.paths)
    # Side effect: folds the option/token headers into req.headers, exactly as
    # the SDK transport does before sending.
    _build_header(req, option, conf)
    body_json = JSON.marshal(req.body) if req.body is not None else None
    json_, files, data = None, None, None
    if req.files:
        files = req.files
        if body_json is not None:
            data = json.loads(body_json)
    elif body_json is not None:
        json_ = json.loads(body_json)
    response = await client.request(
        str(req.http_method.name),
        url,
        headers=req.headers,
        params=tuple(req.queries),
        json=json_,
        data=data,
        files=files,
        timeout=conf.timeout,
    )
    resp = RawResponse()
    resp.status_code = response.status_code
    resp.headers = dict(response.headers)
    resp.content = response.content
    return resp


class LarkInk(Ink[LarkOutboundMessage]):
    app_id: str
    app_secret: SecretStr
    client: lark.Client
    # `client`'s own Config — the key this ink's requests route by in `pools`.
    config: Config
    http: httpx.AsyncClient | None

    def __init__(self, app_id: str, app_secret: SecretStr) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret.get_secret_value())
            .build()
        )
        config = self.client.config
        if config is None:
            raise ValueError("LarkInk: lark.Client was built without a Config")
        self.config = config
        self.http = None

    async def __aenter__(self) -> Self:
        """Open this ink's own pooled client and route the SDK calls its
        `lark.Client` makes onto it (see `pooled_aexecute`)."""
        self.http = httpx.AsyncClient()
        pools[self.config] = self.http
        Transport.aexecute = staticmethod(pooled_aexecute)
        return self

    async def __aexit__(self, *exc: object) -> None:
        del pools[self.config]
        if not pools:
            Transport.aexecute = staticmethod(SDK_AEXECUTE)
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def inspect(self) -> LarkUserProfile:
        # The bot-info endpoint has no async SDK method, so call it over async
        # httpx. A sync client here would block Octomate's event loop — freezing
        # the startup probe and every channel entered after Lark in the lifespan.
        async with httpx.AsyncClient(base_url="https://open.feishu.cn") as http:
            token_resp = await http.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret.get_secret_value(),
                },
            )
            token_resp.raise_for_status()
            token = token_resp.json().get("tenant_access_token")
            if not token:
                raise RuntimeError("LarkInk: failed to obtain tenant_access_token")
            resp = await http.get(
                "/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            bot = resp.json().get("bot", {})
        if not bot:
            raise RuntimeError("LarkInk: inspect failed, no bot info returned")
        return LarkUserProfile(
            channel_user_id=bot.get("open_id", ""),
            name=bot.get("app_name", ""),
            avatar_url=bot.get("avatar_url", ""),
        )

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
                # The SDK user's own `user_id` is Lark's third id namespace
                # (granted by its own scope) — never ours: the registry keys
                # Lark by open_id, and on the schema `user_id` is the owner FK.
                attrs = {
                    key: value
                    for key, value in vars(resp.data.user).items()
                    if value is not None and key != "user_id"
                }
                profile = LarkUserProfile.model_validate(attrs)
                if profile.name:
                    return profile
            else:
                logger.warning(
                    "LarkInk: get_user_profile failed: %s %s",
                    resp.code,
                    resp.msg,
                )
        except Exception:
            logger.warning(
                "LarkInk: get_user_profile failed for %s", user_id, exc_info=True
            )
        return LarkUserProfile(channel_user_id=user_id, name=user_id)

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
        try:
            resp = await self.client.im.v1.image.acreate(request)  # type: ignore[union-attr]
            if resp.success() and resp.data and resp.data.image_key:
                return resp.data.image_key
            logger.warning("LarkInk: image upload failed: %s %s", resp.code, resp.msg)
        except Exception:
            logger.warning("LarkInk: image upload failed", exc_info=True)
        return None

    async def download_media(
        self,
        message_id: str,
        *,
        file_key: str,
    ) -> tuple[bytes, str] | None:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("image")
            .build()
        )
        try:
            resp = await self.client.im.v1.message_resource.aget(request)  # type: ignore[union-attr]
            if not resp.success():
                logger.warning(
                    "LarkInk: download_media failed: %s %s", resp.code, resp.msg
                )
                return None
            file_name: str = resp.file_name or file_key
            data = resp.file.read() if resp.file else b""
            return data, file_name
        except Exception:
            logger.warning("LarkInk: download_media failed", exc_info=True)
            return None

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        if not seg.data.file:
            return None
        result = await self.download_media(message_id, file_key=seg.data.file)
        if result is None:
            return None
        data, file_name = result
        return DownloadedImage(data=data, file_name=file_name)

    async def open_dm(self, user_id: str, opener: str | None = None) -> str | None:
        """A user's own open_id is their 1:1 chat id, so nothing has to be opened."""
        return user_id or None

    @lark_logfire.instrument("lark.send_message", extract_args=["chat_id", "chat_type"])
    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[LarkOutboundMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        # Read from the id we were handed, not from `chat_type`: a p2p address keys on
        # the person's open_id, and a topic reply inside one still does even though it
        # calls itself a thread. Lark's own prefixes are what the feelers already sort
        # `om_` replies by.
        receive_id_type = "open_id" if chat_id.startswith("ou_") else "chat_id"
        first_msg_id: IMMessageID | None = None
        for msg in messages:
            try:
                if reply_to:
                    msg_id = await self.reply_message(
                        reply_to,
                        msg.msg_type,
                        msg.content,
                        reply_in_thread=reply_in_thread,
                    )
                    if not reply_in_thread:
                        reply_to = None
                else:
                    msg_id = await self._create_message(
                        chat_id,
                        receive_id_type,
                        msg.msg_type,
                        msg.content,
                    )
                first_msg_id = first_msg_id or msg_id
            except Exception:
                logger.warning("LarkInk: send_message failed", exc_info=True)
        return first_msg_id

    async def send_text_message(
        self,
        chat_id: str,
        chat_type: str,
        text: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        return await self.send_message(
            chat_id,
            chat_type,
            [
                LarkOutboundMessage(
                    msg_type="text",
                    content=json.dumps(
                        {"text": text}, ensure_ascii=False, separators=(",", ":")
                    ),
                )
            ],
            reply_to,
            reply_in_thread=reply_in_thread,
        )

    @lark_logfire.instrument("lark.create_stream_card", extract_args=False)
    async def create_stream_card(
        self,
        card_data: str,
        *,
        element_id: str,
    ) -> LarkStreamCard:
        request = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(card_data)
                .build()
            )
            .build()
        )
        # Let both the SDK exception and the API error reason surface to the
        # caller — swallowing them to None hid the real cause behind a generic
        # "failed to create card" upstream.
        resp = await self.client.cardkit.v1.card.acreate(request)  # type: ignore[union-attr]
        if resp.success() and resp.data and resp.data.card_id:
            return LarkStreamCard(card_id=resp.data.card_id, element_id=element_id)
        raise RuntimeError(f"Lark create stream card failed: {resp.code} {resp.msg}")

    async def send_stream_card(
        self,
        chat_id: str,
        chat_type: str,
        card: LarkStreamCard,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        msg = LarkOutboundMessage(
            msg_type="interactive",
            content=json.dumps(
                {"type": "card", "data": {"card_id": card.card_id}},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return await self.send_message(
            chat_id,
            chat_type,
            [msg],
            reply_to,
            reply_in_thread=reply_in_thread,
        )

    async def update_stream_card(
        self,
        card: LarkStreamCard,
        *,
        content: str,
        sequence: int,
    ) -> bool:
        with lark_logfire.span(
            "lark.update_stream_card", chars=len(content), sequence=sequence
        ):
            request = (
                ContentCardElementRequest.builder()
                .card_id(card.card_id)
                .element_id(card.element_id)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .uuid(str(uuid7()))
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            try:
                resp = await self.client.cardkit.v1.card_element.acontent(request)  # type: ignore[union-attr]
                if resp.success():
                    return True
                logger.warning(
                    "LarkInk: update stream card failed: %s %s",
                    resp.code,
                    resp.msg,
                )
            except Exception:
                logger.warning("LarkInk: update stream card failed", exc_info=True)
            return False

    @lark_logfire.instrument("lark.finish_stream_card", extract_args=["sequence"])
    async def finish_stream_card(
        self,
        card: LarkStreamCard,
        *,
        sequence: int,
    ) -> bool:
        """Disable streaming_mode so the card settles (typewriter stops, the
        card becomes interactive). Best-effort: a failure only leaves the card
        locked until Lark's ~10 minute auto-timeout."""
        settings = json.dumps(
            {"config": {"streaming_mode": False}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request = (
            SettingsCardRequest.builder()
            .card_id(card.card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(settings)
                .uuid(str(uuid7()))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        try:
            resp = await self.client.cardkit.v1.card.asettings(request)  # type: ignore[union-attr]
            if resp.success():
                return True
            logger.warning(
                "LarkInk: finish stream card failed: %s %s",
                resp.code,
                resp.msg,
            )
        except Exception:
            logger.warning("LarkInk: finish stream card failed", exc_info=True)
        return False

    async def patch_card(self, message_id: str, content: str) -> bool:
        """Replace the card of an already-sent interactive message in place."""
        with lark_logfire.span("lark.patch_card", chars=len(content)):
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder().content(content).build()
                )
                .build()
            )
            try:
                resp = await self.client.im.v1.message.apatch(request)  # type: ignore[union-attr]
                if resp.success():
                    return True
                logger.warning("LarkInk: patch card failed: %s %s", resp.code, resp.msg)
            except Exception:
                logger.warning("LarkInk: patch card failed", exc_info=True)
            return False

    async def _create_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
    ) -> IMMessageID | None:
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
            logger.warning("LarkInk: create message failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .reply_in_thread(reply_in_thread)
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.areply(request)  # type: ignore[union-attr]
        if not resp.success():
            logger.warning("LarkInk: reply message failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None
