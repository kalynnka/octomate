"""Shared dsh Remote API envelopes for the CLI and server."""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import (
    AliasGenerator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
)
from pydantic.alias_generators import to_camel
from typing_extensions import TypedDict

# Wire names are camelCase; models keep snake_case attributes behind generated
# validation/serialization aliases, and `populate_by_name` keeps construction
# from Python readable. Deliberately not a plain `alias`: that would also rename
# the synthesized `__init__` parameters under pyright, and the two halves are
# all the wire needs. `extra="allow"` is the carried-not-rejected posture: an
# unknown field survives a round-trip into replay metadata instead of vanishing.
PERMISSIVE = ConfigDict(
    extra="allow",
    populate_by_name=True,
    alias_generator=AliasGenerator(
        validation_alias=to_camel, serialization_alias=to_camel
    ),
)


class RpcError(BaseModel):
    """A Remote call's error: its code, message, and details."""

    model_config = PERMISSIVE

    code: str
    message: str
    details: JsonValue = None


class OkResult(BaseModel):
    """A successful Remote result and its value."""

    model_config = PERMISSIVE

    ok: Literal[True] = True
    value: JsonValue = None


class ErrResult(BaseModel):
    """A failed Remote result and its error."""

    model_config = PERMISSIVE

    ok: Literal[False] = False
    error: RpcError


# Not discriminated: `ok` is a bool literal, which pydantic's smart union already
# matches exactly, and the pair has no other overlap.
type RpcResult = OkResult | ErrResult


class ClientRequest(BaseModel):
    """POST /api/<method> body."""

    model_config = PERMISSIVE

    type: Literal["client-request"] = "client-request"
    rpc_id: str
    method: str
    payload: JsonValue = None


class ServerResponse(BaseModel):
    """The body that POST answers with. A response echoes its request's rpcId."""

    model_config = PERMISSIVE

    type: Literal["server-response"] = "server-response"
    rpc_id: str
    result: RpcResult


class RemoteItem(BaseModel):
    """One item on a multiplexed Remote stream."""

    model_config = PERMISSIVE

    type: Literal["item"]
    stream_id: str
    value: JsonValue = None


class SessionAddress(TypedDict):
    kind: Literal["session"]
    sessionId: str


class SessionFollowRequest(TypedDict):
    address: SessionAddress
    maxMessages: int
    assistantStream: NotRequired[bool]


class SessionFollowArgs(TypedDict):
    request: SessionFollowRequest


class SessionFollowPayload(TypedDict):
    args: SessionFollowArgs


class RemoteSessionFollow(BaseModel):
    model_config = PERMISSIVE

    type: Literal["open"] = "open"
    stream_id: str
    endpoint: Literal["session/follow"] = "session/follow"
    payload: SessionFollowPayload


class RemoteError(BaseModel):
    """An error on a multiplexed Remote stream."""

    model_config = PERMISSIVE

    type: Literal["error"]
    stream_id: str
    error: RpcError


class RemoteEnd(BaseModel):
    """The end of a multiplexed Remote stream."""

    model_config = PERMISSIVE

    type: Literal["end"]
    stream_id: str


remote_message_adapter = TypeAdapter(
    Annotated[RemoteItem | RemoteError | RemoteEnd, Field(discriminator="type")]
)
