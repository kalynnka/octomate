# Plan: compact cross-agent handoff package

> **Status:** proposed · **Scope:** `summon`, `scheme`, and future agent changes
> **Depends on:** durable thread history and synchronized agent transcripts

## Outcome

A new agent receives a compact, useful working brief without receiving the entire source
conversation as a new prompt prefix. Full history stays available for deliberate lookup
when the brief is insufficient.

## Feature requirements

1. Every cross-agent handoff carries a bounded, self-contained handoff package.
2. The package distinguishes the user's goal, constraints, decisions, completed work,
   open work, important artifacts, and expected result.
3. The source agent includes only context that changes the target agent's next action.
4. Relevant thread messages, runs, files, and external resources can be referenced with
   stable Octomate handles instead of copied in full.
5. The target agent can retrieve referenced history on demand through existing or shared
   history tools.
6. The package records its source agent, source run, target route, reason, and creation
   time for audit and later inspection.
7. User-facing handoff text remains separate from the internal working brief.
8. The same handoff shape works for Inkling, Claude, and Codex.
9. The feature has an explicit size budget and must not silently fall back to embedding
   the complete transcript.

## Direction

- Define one typed handoff document owned by Octomate and persisted with the handoff.
- Provide a shared handoff skill that teaches agents how to prepare concise packages and
  when to reference history rather than repeat it.
- Let Gateway operations accept or create that canonical package instead of carrying an
  unconstrained prose transcript.
- Expose focused history retrieval to the receiving agent so additional context is paid
  for only when needed.
- Preserve the original synchronized sessions and transcripts as the source of truth;
  the handoff is a working index and summary, not another history store.

## Non-goals

- Reconstructing one provider's KV cache inside another provider.
- Automatically copying the complete model timeline into the target agent's prompt.
- Replacing native Claude or Codex subagent delegation.

## Acceptance

- A representative long conversation hands off within the configured package budget.
- The target can begin useful work from the package alone and can resolve omitted detail
  from its references.
- The package and its referenced source remain inspectable after both runs finish.

