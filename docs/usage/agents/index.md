# Agents

Every agent tentacle answers the same calls from Octomate: run a turn for a
conversation, stream what happens, raise approvals and questions, and say which
models it offers. What differs is the runtime behind it.

```yaml
tentacles:
  claude:
    type: claude
    permission_mode: default
  codex:
    type: codex
    permission_mode: user_review
  inkling:
    type: inkling
    models:
      - name: anthropic:claude-sonnet-5
```

Each agent `type` may be declared once, because each owns a fixed hook route. Every
block accepts `enabled` (default `true`) and `gateway` (default `true`): with
`gateway: false`, that agent's driven turns get none of the routing spells.

## Routes and claims

A **route** is one `(agent, model)` pair a channel can reach, and every route carries
a **claim**: what it is for, and which thinking efforts it accepts. Agents advertise;
the caller picks. The answering agent reads the claims when deciding whether to hand
work to somebody better placed.

Claude Code, Codex and DeepSeek Harness report their catalogs at startup, with each
model's description and supported efforts where the harness has them. Inkling's
routes are its configured `models`. Where a harness reports nothing, `claims` fills
the gap, keyed by the provider-qualified model name:

```yaml
tentacles:
  claude:
    type: claude
    claims:
      "anthropic:claude-opus-5":
        ability: Deep review and hard refactors.
        efforts: [medium, high, xhigh]
```

Reported metadata wins over a configured claim. Effort is one vocabulary across every
agent, `minimal`, `low`, `medium`, `high`, `xhigh`, and each tentacle maps it onto
its runtime's own knob. A route whose provider takes less must say so in its claim,
because nothing downgrades an effort the provider lacks.

## Models

A harness agent has no configured default model. Omitting one preserves the
harness's own settings, including the model a resumed session had chosen. Inkling's
default is the first entry in its `models` list. A channel exposes every model of
every agent it binds; the entry agent's default is what answers a new conversation.

[Permissions and approvals](permissions.md) covers the posture each runtime takes
and how a card reaches you. [Driven and native sessions](sessions.md) covers what a
run from a channel switches off, and how a session from your own terminal arrives.
