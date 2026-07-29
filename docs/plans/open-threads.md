# Plan (raw): what is still open after the pydantic-ai v2 / GitHub OAuth work

> **Status:** raw register, nothing designed — captured so none of it is rediscovered ·
> **Owner:** @luhui · **Created:** 2026-07-30 · **Updated:** 2026-07-30
> **Trigger:** shipped `agent/pydantic-ai-v2`; several things were diagnosed but deliberately
> not fixed, and a few were found while fixing something else.

Ordered by what blocks a user today. Each entry says what is actually true, not what was
suspected — where a claim came from a trace or from reading a dependency, that is named.

Closed since this was written: the Lark OAuth confirm button (deleted — the agent confirms in
chat, and it reads better than the button did), and the DeepSeek `reasoning_content` 400 (gone
with the v2 upgrade, confirmed live).

## 1. Lark approvals and questions still need a card callback

The OAuth card no longer depends on one — its confirm button is gone and the agent finishes the
connection instead. Approvals and questions cannot take that exit: **Approve/Deny and the question
forms are the interaction**, and both come back only over Feishu's `card.action.trigger` 回调.

Neither transport is available as things stand:

- **长连接** — enterprise-only on this tenant. Ruled out by the account, not by the code.
- **HTTP 请求地址** — needs a publicly reachable HTTPS endpoint. Not stood up.

So a run that suspends on a Lark approval waits forever, and nobody has noticed because nothing
logs it. That is the sharpest thing in this document.

The handler itself is sound and should not be re-investigated: registration is correct,
`ws_mod.loop` is Octomate's own loop so `asyncio.create_task` schedules on the right one, and
`on_card_action` answers inside Feishu's 3-second budget by returning the toast immediately and
doing the slow work in a task. It now also logs the action it received and warns on the two paths
that used to return an empty response in silence — so nothing in the log on a press means the
callback never arrived, while a line means delivery is solved and the problem moved.

One dead end worth not walking twice: the SDK's `MessageType.CARD` branch in
`lark_oapi/ws/client.py` discards frames, which looks like the culprit and is not. Card callbacks
travel the `EVENT` branch — `_do_without_validation` resolves `p2.card.action.trigger` out of
`_callback_processor_map`, and the ws client base64-encodes the handler's return value back as the
response, a path that only makes sense for a card callback. lark-oapi's own channel abstraction
registers it the same way. Verified identical in 1.6.2 and 1.7.1, so an upgrade changes nothing.

Directions, undecided:

- Mount a FastAPI route as the card callback URL. Architecturally endorsed — web APIs are already
  routers on the project-level Octomate instance — but needs public ingress.
- Give approvals and questions a chat-answered path the way OAuth just got one, so Lark degrades
  to what napcat already does instead of hanging.
- Accept that they are dead on Lark until ingress exists, and make a suspended run say so rather
  than waiting silently.

## 2. MCP toolsets cannot reconnect

`MCPToolset.__aenter__` only dials when its refcount is zero, and the tentacle's startup warm pins
it above zero for the process lifetime. A dropped Linear session is therefore fatal until restart —
nothing re-dials, and the toolset keeps answering from a dead session.

Byte-identical in 1.107, so this is not migration fallout. Untouched.

## 3. Slack sends are lossy

One immediate retry — the SDK default — then the reply is dropped with a warning and the user
never learns their answer went nowhere. This is not theoretical here: the same tunnel that produced
§4 resets mid-handshake often enough to matter.

## 4. The tunnel breaks TLS, and failures did not say so

Trace `019fae46660fcab91f4cbef127738bc6` (2026-07-29 14:28 UTC) was a `connect_github` that died in
`stream.start_tls` with an empty-message `httpx.ConnectError` — no request ever sent. Slack calls
from the same process failed in the same 30 seconds, one of them explicitly:

```
SSLCertVerificationError: certificate verify failed: Hostname mismatch,
certificate is not valid for 'slack.com'
```

A bare reset on one host plus a cert-hostname mismatch on another, concurrently, is local TLS
interception — the Clash tunnel. Not a code defect, but it means outbound failures are routine
here, and the code should read as though they are.

The reporting half is fixed: `ToolFailureCapability` turns the raised `ConnectError` into a result
the model can explain, where before the turn died as `UnexpectedModelBehavior: Tool
'connect_github' exceeded max retries count of 1` with the cause dropped on the floor.

## 5. Smaller, known, accepted

- **Approval cards show their backticks.** [`approvals.py`](../../octomate/tentacles/channel/lark/feelers/approvals.py)
  formats `` `tool_name` `` and a ```` ```json ```` fence, and Lark's card markdown has no code
  span — the same bug fixed on the OAuth card. Left alone deliberately.
- **Two concurrent `connect_github` calls mint two device codes.** `start` reads then inserts with
  no lock. `live_device_authorization` orders by uuid7 `id desc`, so the newer wins and the older
  sits unconsumed until it expires. Nothing breaks; one code is wasted. Same race class as
  [manager-ensure-and-cache](./manager-ensure-and-cache.md).
- **Three `tests/test_config.py` failures are environmental.** They assert against defaults the
  local `octomate.yaml` deliberately overrides, so they fail on this machine and pass in CI.
  Either make them independent of local config or stop pretending they are signal.
- **`DeviceOperationPayload` changed the shape of `encrypted_data`.** Any authorization pending
  across that deploy fails its next confirm with a validation error. Loud, not silent, and they
  expire within the quarter hour — but it is the reason for an otherwise baffling error once.
- **Two napcat `test_chromo` tests fail in isolation** and pass in the full suite — a Pydantic
  `class-not-fully-defined` that resolves once another module imports first. Pre-existing.
