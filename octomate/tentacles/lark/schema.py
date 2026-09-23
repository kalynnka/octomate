from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, Protocol, runtime_checkable
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    RootModel,
    Tag,
    model_validator,
    with_config,
)
from typing_extensions import TypedDict

from octomate.schemas.deferred import DeferredQuestion
from octomate.schemas.user import UserProfile
from octomate.types.json import JsonObject

NonEmptyStr = Annotated[str, Field(min_length=1)]


@runtime_checkable
class LarkAvatar(Protocol):
    avatar_origin: str | None


type LarkProfileValue = str | int | bool | JsonObject | LarkAvatar | None
type LarkProfileData = dict[str, LarkProfileValue]


class LarkApprovalActionValue(TypedDict):
    action: NonEmptyStr
    batch_id: UUID
    action_id: UUID
    tool_name: NotRequired[str]


class LarkQuestionActionValue(TypedDict):
    action: NonEmptyStr
    batch_id: UUID
    questions: Annotated[list[DeferredQuestion], Field(min_length=1)]
    page: int
    answers: dict[UUID, str]
    choice: NotRequired[str]


class LarkQuestionFormValue(TypedDict, total=False):
    answer: str | None
    choice: str | None


@dataclass(frozen=True)
class LarkOutboundMessage:
    msg_type: str
    content: str


@dataclass(frozen=True)
class LarkStreamCard:
    card_id: str
    element_id: str


class LarkTextContent(BaseModel):
    text: str


class LarkImageContent(BaseModel):
    image_key: str


class LarkCardReferenceData(TypedDict):
    card_id: str


class LarkCardReference(BaseModel):
    type: Literal["card"] = "card"
    data: LarkCardReferenceData


@with_config(ConfigDict(extra="allow"))
class LarkCardSummary(TypedDict):
    content: str


@with_config(ConfigDict(extra="allow"))
class LarkPrintSettings(TypedDict):
    default: int
    android: int
    ios: int
    pc: int


@with_config(ConfigDict(extra="allow"))
class LarkStreamingOptions(TypedDict):
    print_frequency_ms: LarkPrintSettings
    print_step: LarkPrintSettings
    print_strategy: Literal["fast"]


@with_config(ConfigDict(extra="allow"))
class LarkStreamingConfig(TypedDict):
    streaming_mode: bool
    summary: NotRequired[LarkCardSummary]
    streaming_config: NotRequired[LarkStreamingOptions]


class LarkCardSettings(BaseModel):
    config: LarkStreamingConfig


@with_config(ConfigDict(extra="allow"))
class LarkMarkdownElement(TypedDict):
    tag: Literal["markdown"]
    content: str
    element_id: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class LarkCardText(TypedDict):
    tag: Literal["plain_text", "markdown", "lark_md"]
    content: str


@with_config(ConfigDict(extra="allow"))
class LarkCardHeader(TypedDict):
    title: LarkCardText
    template: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class LarkDivider(TypedDict):
    tag: Literal["hr"]


@with_config(ConfigDict(extra="allow"))
class LarkButton(TypedDict):
    tag: Literal["button"]
    text: LarkCardText
    type: NotRequired[str]
    value: NotRequired[JsonObject]
    action_type: NotRequired[str]
    url: NotRequired[str]
    name: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class LarkActionRow(TypedDict):
    tag: Literal["action"]
    actions: list[LarkButton]


@with_config(ConfigDict(extra="allow"))
class LarkInput(TypedDict):
    tag: Literal["input"]
    name: str
    placeholder: NotRequired[LarkCardText]
    default_value: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class LarkForm(TypedDict):
    tag: Literal["form"]
    name: str
    elements: list[LarkCardElement]


@with_config(ConfigDict(extra="allow"))
class LarkColumn(TypedDict):
    tag: Literal["column"]
    elements: list[LarkCardElement]


@with_config(ConfigDict(extra="allow"))
class LarkColumnSet(TypedDict):
    tag: Literal["column_set"]
    columns: list[LarkColumn]


@with_config(ConfigDict(extra="allow"))
class LarkCollapsiblePanel(TypedDict):
    tag: Literal["collapsible_panel"]
    expanded: bool
    header: LarkCardHeader
    elements: list[LarkCardElement]


type LarkCardElement = Annotated[
    LarkMarkdownElement
    | LarkDivider
    | LarkButton
    | LarkActionRow
    | LarkInput
    | LarkForm
    | LarkColumn
    | LarkColumnSet
    | LarkCollapsiblePanel,
    Field(discriminator="tag"),
]


@with_config(ConfigDict(extra="allow"))
class LarkCardBody(TypedDict):
    elements: list[LarkCardElement]


class LarkCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    header: LarkCardHeader | None = None
    schema_version: Literal["2.0"] = Field(default="2.0", alias="schema")
    config: LarkStreamingConfig | None = None
    body: LarkCardBody


class LarkLegacyCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    header: LarkCardHeader | None = None
    elements: list[LarkCardElement]


def lark_card_kind(value: LarkCard | LarkLegacyCard | JsonValue) -> str:
    if isinstance(value, LarkCard) or (isinstance(value, dict) and "schema" in value):
        return "v2"
    return "legacy"


class LarkInteractiveCard(
    RootModel[
        Annotated[
            Annotated[LarkCard, Tag("v2")] | Annotated[LarkLegacyCard, Tag("legacy")],
            Discriminator(lark_card_kind),
        ]
    ]
):
    pass


class LarkRawCard(RootModel[JsonObject]):
    """An externally supplied card, carried without interpreting its schema."""


class LarkBotInfo(BaseModel):
    open_id: NonEmptyStr
    app_name: str = ""
    avatar_url: str = ""


class LarkBotInfoResponse(BaseModel):
    bot: LarkBotInfo


class LarkUserProfile(UserProfile):
    model_config = ConfigDict(extra="ignore")

    channel_user_id: str = Field(default="", validation_alias="open_id")
    title: str | None = Field(default=None, validation_alias="job_title")

    union_id: str = ""
    en_name: str = ""
    avatar_url: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: LarkProfileData) -> LarkProfileData:
        if isinstance(data, dict):
            avatar = data.pop("avatar", None)
            avatar_origin = None
            if isinstance(avatar, dict):
                raw_avatar_origin = avatar.get("avatar_origin")
                if isinstance(raw_avatar_origin, str):
                    avatar_origin = raw_avatar_origin
            elif isinstance(avatar, LarkAvatar):
                avatar_origin = avatar.avatar_origin
            if avatar_origin:
                data.setdefault("avatar_url", avatar_origin)
            gender = data.get("gender")
            if isinstance(gender, int):
                data["gender"] = {1: "male", 2: "female", 3: "other"}.get(gender)
            if not data.get("name"):
                nickname = data.get("nickname")
                en_name = data.get("en_name")
                data["name"] = (
                    nickname
                    if isinstance(nickname, str)
                    else en_name
                    if isinstance(en_name, str)
                    else ""
                )
        return data
