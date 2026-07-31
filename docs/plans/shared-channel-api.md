# Plan: shared semantic channel API for all agents

> **Status:** proposed · **Scope:** structured context, discovery, and channel actions
> **Builds on:** channel tentacles, Feelers, Chromo, `Ink`, identity, and thread history

## Outcome

Every agent can understand its current human/channel context and perform supported
channel interactions through one Octomate-owned semantic API. Platform transport remains
inside channel tentacles.

## Boundary

`Ink` remains the platform API client and is not exposed to agents. The shared API sits
above channel tentacles and uses existing Feelers, Chromo, typed message segments, user
identity, and destination resolution.

Gateway continues to own agent routing and Reflex continuation. The channel API owns
channel context and concrete interaction; it must not create a second routing system.

## Feature requirements

1. Every driven run can discover a structured snapshot of its current channel, thread,
   agent, user, and bot identity.
2. User information is resolved through Octomate's canonical identity registry and
   exposes safe handles rather than credentials or unrestricted platform identifiers.
3. Discovery reports only operations and content forms supported by the active channel.
4. Agents can address approved destinations using the same opaque handles Gateway uses.
5. Initial sending accepts the existing typed message segments and behaves consistently
   across streaming and non-streaming channels.
6. A successful action is recorded, delivered, and observable in the shared event and
   thread models.
7. Mutating operations are authorized by the active user, channel-agent policy, and
   channel surface.
8. Runtime context is isolated per run so concurrent sessions cannot read or act through
   one another's channel identity.
9. The API is available to Inkling, Claude, and Codex through a standard tool/MCP surface
   or an equivalent thin provider adapter.
10. Unsupported operations fail clearly instead of falling back to guessed platform
    behavior.

## Direction

- Introduce an Octomate-owned channel interaction service above tentacles and Feelers.
- Begin with context discovery, capability discovery, and segment sending.
- Reuse Gateway's destination directory rather than building another address resolver.
- Bind each invocation to a short-lived run context carrying the effective agent, user,
  channel, conversation, and permissions.
- Extend declared channel surfaces only when a neutral operation has real channel
  implementations.
- Add reactions, edits, deletion, uploads, or richer interactions incrementally after
  their common semantics are settled.

## Initial scope cut

- Included: safe context, discovery, destinations, and sending.
- Postponed: reactions, edits, deletion, arbitrary history search, and platform-specific
  administrative APIs.
- Never exposed: raw channel clients, authentication secrets, or arbitrary chat/user ids.

## Acceptance

- Each agent receives the same semantic context for the same run.
- Discovery accurately differs between channels with different surfaces.
- Sending records and presents one message without duplicate delivery.
- An invalid destination, expired run context, or unsupported operation is rejected
  before reaching the platform client.
