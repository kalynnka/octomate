# Inkling

Inkling is the agent Octomate runs itself: an in-process
[Pydantic AI](https://ai.pydantic.dev/) agent with no harness behind it. It is the
fallback for any model Pydantic AI supports, and the chat-side generalist when the
work does not need a coding harness.

## Enable

Choose a model supported by your provider and add it under `models` in the block
below. Supply its [provider credentials](#providers), bind `inkling` in a channel's
`agents` list, and [check and restart Octomate](../../tentacles/index.md#enable-a-tentacle).
No separate harness or native hooks are needed; verify it by sending a message
through the channel.

```yaml
tentacles:
  inkling:
    type: inkling
    models:
      - name: anthropic:claude-sonnet-5
      - name: anthropic:claude-opus-5
        settings:
          thinking: high
    claims:
      "anthropic:claude-sonnet-5":
        ability: General assistant for everyday questions and coordination.
    permission_mode: default        # default | dontAsk | bypassPermissions
    request_limit: 256              # model requests per run
```

`models` is required and its first entry is the default. Every configured model is
a route on any channel that binds the agent. `settings` is a per-model Pydantic AI
`ModelSettings`, layered over the provider's defaults.

## Providers

A model name is `<provider>:<model>`, with providers `openai`, `openai-chat`,
`deepseek`, `google`, `google-cloud`, `anthropic` and `bedrock`. Each resolves
through `providers.yaml`, and an omitted block falls back to the provider's own
environment variables, `ANTHROPIC_API_KEY` and the like. Supply keys in the YAML
provider block or `OCTOMATE__PROVIDERS__<NAME>__API_KEY` environment variables;
`.env` is optional.

```yaml
providers:
  anthropic:
    api_key: sk-ant-...
  openai:
    base_url: https://api.openai.com/v1
  vertex:
    project: my-gcp-project
    location: global
  bedrock:
    region_name: us-east-1
```

Provider blocks also carry model settings applied to every model they build, which
is where prompt caching is turned on by default for Anthropic, Bedrock and OpenAI.

## What it can do

Every Inkling run carries:

- `ask_questions`, the one way it asks the user something, batched.
- The todo tools, for planning multi-step work, rendered as checklists on channels.
- History search over every thread the person it answers has spoken in.
- The routing spells, plus two of its own: `commission`, which puts another agent to
  work in the background and returns its report, and `whisper`, which follows up
  with that accomplice. See [Moving a conversation](../gateway.md).
- The person's installed [MCP connectors](../mcp/proxy.md), acting as them.

In a thread bound to a [project](../projects.md) it also gets file, shell and
repository-context tools rooted in the workspace, each behind approval, with the
provider keys the process runs on hidden from the shell. With no project it has no
workspace and no file tools at all: it is a conversation.

## Postures and deferrals

Inkling is the one agent that can resolve a deferral without a human and keep going:

- `default` parks every approval and question for a person.
- `dontAsk` answers questions itself and denies approvals, and says in its report
  what it assumed.
- `bypassPermissions` grants approvals without a card and still decides questions
  itself.

A commissioned run declines everything, since it has no user. Because Inkling's
deferrals go through the persisted graph rather than a parked process, an answer to
an Inkling question survives a restart.

## Oversized tool output

A tool return too large to sit in the context is cut down before it reaches the
model, and stays cut down for the rest of the conversation. By default anything
over ten thousand characters is **spilled**: stored whole, with a preview and a
handle the model can read back on demand. Past a hundred thousand it is
**summarised** by the run's own model. Either falls back to truncation. Spills
outlive their run for six hours by default.

```yaml
tentacles:
  inkling:
    tool_output:
      enabled: true
      over_tokens: false
      retention_hours: 6
      bands:
        - over: 10000
          action: {kind: spill, preview_chars: 1000}
        - over: 100000
          action: {kind: summarize}
```
