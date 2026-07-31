# Plan: shared Gateway for every agent tentacle

> **Status:** proposed · **Scope:** Inkling, Claude, and Codex driven by Octomate
> **Builds on:** the shipped Reflex loop, native session ingest, and Gateway tools

## Outcome

Every agent tentacle can use the same Octomate Gateway behavior while retaining its
native SDK, session, transcript, and event adapter. Gateway policy belongs to Octomate;
only its tool projection is provider-specific.

## Existing premises

- Claude and Codex session histories are already captured through hooks and local
  transcript synchronization. Shared Gateway work must preserve and reuse that model.
- Reflex remains the owner of routing, handoff, continuation, persistence, and channel
  presentation.
- `teleport` is a Reflex action for the same agent and same session/context. It changes
  where subsequent run events are presented; it does not create a cross-agent handoff
  or resend the conversation.
- `commission` and `whisper` are Inkling's Octomate subagent facilities. They are not
  required for initial Claude/Codex Gateway parity because those agents already have
  native subagent systems.

## Feature requirements

1. `scry`, `summon`, `scheme`, `teleport`, and `send` expose the same user-facing
   meaning and enforce the same Octomate policies for all three agents.
2. Route and destination discovery comes from the existing channel-agent matrix and
   identity registry. Agents name safe handles, never platform addresses.
3. Reflex consumes the same typed routing decisions regardless of which agent produced
   them.
4. `teleport` preserves the active agent session/context and redirects presentation to
   the destination chosen by Reflex.
5. Gateway actions and their results remain visible in the shared run event stream and
   durable thread history.
6. Provider-specific failures are translated into useful tool errors without changing
   Gateway policy.
7. Gateway availability is controlled per channel-agent connection; installing an
   agent does not implicitly enable it on every channel.
8. Inkling behavior remains compatible throughout the extraction.

## Direction

- Separate Gateway's route resolution, destination resolution, validation, and typed
  decisions from its Pydantic AI toolset.
- Keep one Octomate-owned Gateway session per driven turn.
- Give Inkling, Claude, and Codex thin adapters that project the shared operations into
  their native tool mechanisms and translate results back into the common event model.
- Keep terminal actions in the Reflex loop: agent adapters report the decision; Reflex
  performs the handoff or presentation move.
- Reuse the existing session/tailer synchronization instead of introducing a second
  conversation-history mechanism.
- Add cross-agent behavioral tests around the common decisions rather than duplicating
  provider-specific Gateway test suites.

## Initial scope cut

- Shared: `scry`, `summon`, `scheme`, `teleport`, and `send`.
- Postponed: exposing Octomate `commission` and `whisper` to Claude or Codex.
- Not included: a universal replacement for every Pydantic AI capability or proxying
  third-party MCP servers through Gateway.

## Acceptance

- The same routing scenario produces the same decision from Inkling, Claude, and Codex.
- A teleport continues the same agent session and presents its continuation in the new
  Reflex-selected location.
- A summon changes ownership and starts the target agent from a bounded handoff package.
- Existing Inkling Gateway and native Claude/Codex transcript tests remain green.

