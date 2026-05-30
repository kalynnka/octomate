from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, Json
from typing_extensions import NotRequired, TypedDict

from octomate.schemas.conversation import UserProfile
from octomate.schemas.deferred import DeferredQuestion

NonEmptyStr = Annotated[str, Field(min_length=1)]


class SlackFileInfo(TypedDict, total=False):
    mimetype: str
    url_private: str
    name: str


class SlackMessageEvent(TypedDict, total=False):
    type: Literal["message"]
    subtype: str
    bot_id: str
    user: str
    team: str
    team_id: str
    enterprise_id: str
    channel: str
    channel_type: str
    ts: str
    thread_ts: str
    text: str
    files: list[SlackFileInfo]


class SlackAssistantThread(TypedDict, total=False):
    user_id: str
    channel_id: str
    thread_ts: str
    context: dict[str, Any]


class SlackAssistantThreadEvent(TypedDict, total=False):
    type: Literal["assistant_thread_started", "assistant_thread_context_changed"]
    assistant_thread: SlackAssistantThread
    event_ts: str


class SlackPostMessageKwargs(TypedDict):
    channel: str
    text: str
    blocks: NotRequired[list[dict[str, Any]]]
    thread_ts: NotRequired[str]


class SlackActionUser(TypedDict):
    id: NonEmptyStr


class SlackActionChannel(TypedDict):
    id: NonEmptyStr


class SlackActionMessage(TypedDict):
    ts: NonEmptyStr


class SlackApprovalActionValue(TypedDict):
    batch_id: UUID
    action_id: UUID
    approved: bool
    tool_name: NotRequired[str]


class SlackQuestionActionValue(TypedDict):
    batch_id: UUID
    questions: Annotated[list[DeferredQuestion], Field(min_length=1)]
    page: int
    answers: dict[UUID, str]
    selected_answer: NotRequired[str]


class SlackApprovalBlockAction(TypedDict):
    action_id: NonEmptyStr
    value: Json[SlackApprovalActionValue]


class SlackQuestionBlockAction(TypedDict):
    action_id: NonEmptyStr
    value: Json[SlackQuestionActionValue]


class SlackPlainTextInputState(TypedDict, total=False):
    value: str | None


class SlackStaticSelectOption(TypedDict):
    value: str


class SlackStaticSelectState(TypedDict, total=False):
    selected_option: SlackStaticSelectOption | None


class SlackQuestionStateBlock(TypedDict, total=False):
    octomate_question_answer: SlackPlainTextInputState
    octomate_question_choice: SlackStaticSelectState


class SlackQuestionState(TypedDict):
    values: dict[str, SlackQuestionStateBlock]


class SlackApprovalActionBody(TypedDict):
    actions: Annotated[tuple[SlackApprovalBlockAction, ...], Field(min_length=1)]
    user: SlackActionUser
    channel: SlackActionChannel
    message: SlackActionMessage


class SlackQuestionActionBody(TypedDict):
    actions: Annotated[tuple[SlackQuestionBlockAction, ...], Field(min_length=1)]
    state: SlackQuestionState
    user: SlackActionUser
    channel: SlackActionChannel
    message: SlackActionMessage


@dataclass(frozen=True)
class SlackOutboundMessage:
    text: str
    markdown_text: str | None = None
    blocks: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class SlackThreadContext:
    thread_ts: str
    recipient_user_id: str | None = None
    recipient_team_id: str | None = None


class SlackUserProfile(UserProfile):
    user_id: str = ""
    name: str = ""
    nickname: str | None = Field(default=None)
    title: str | None = None
