# Documentation

The site is MkDocs Material for the guide and mkdocstrings for the API reference,
built from `docs/` and published from `main` by the `docs.yml` workflow. This page
is the contract for anyone, person or agent, who changes it.

## The shape

Seven tabs, each answering one kind of question. A fact belongs in exactly one of
them; the others link to it.

| Tab | Question it answers | Written for |
|---|---|---|
| **Installation** | How do I get a server running and my agent connected? | An operator, or their assistant, doing it once |
| **Tentacles** | Which integrations are available, and how do I enable them? | Someone connecting agents, channels and tools |
| **Usage** | How do I work across the connections I enabled? | Someone who has it running |
| **Concepts** | Why is it built this way? | A developer about to read the code |
| **Contributing** | How do I change it? | A developer about to write code |
| **API Reference** | What exactly is this symbol? | Generated from docstrings; never hand-written |
| **Home** | What is this? | Everyone, once |

Each tab opens with an overview page that introduces the tab and nothing more. The
substance sits in the sections beneath it, which are what the sidebar shows:

| Tab | Sections |
|---|---|
| Installation | Server · Configuration · Client · Operations |
| Tentacles | Agents · Channels · MCP connectors |
| Usage | Sessions and permissions · Conversations · Projects and workspaces · MCP |
| Concepts | Design · Storage |
| Contributing | Development · Extending Octomate · Project workflow |
| API Reference | Configuration · Tentacle interfaces · Reflex graph · Managers · Schemas · Capabilities · MCP and OAuth · CLI · Protocol |

The Tentacles tab owns the integration catalog and enablement guides. Existing
agent and channel pages retain their paths under `usage/agents/` and
`usage/channels/` so their URLs stay stable. Each family has an overview, with
Inkling last among agents. Shared session and permission guides stay in Usage.
A page that belongs to no section, such as Accounts and
tokens or Vocabulary, sits between sections at the top level. A new page joins an
existing section. A new section is a structural change: propose it before writing
it, and add its row here.

Page templates, so pages of one kind stay parallel:

- **A channel page**: Create the app · Configure · Where things land · Rendering ·
  Profile linking · Limits. Its row goes in the render matrix on the channels
  overview and in the table on the threads page.
- **An agent page**: the config block · Driven runs · Approvals and questions ·
  Native sessions · Not yet. Its row goes in the tables on the agents overview.
- **An installation page**: the steps in the order they are run, what each writes,
  and how to verify. Commands in fenced blocks with the working directory stated.
- **A usage page**: what the user wants to do, the steps or an example request,
  what to expect, and practical limits. Configuration belongs in Installation or
  Tentacles; storage, transport and runtime design belong in Concepts; tool
  signatures belong in the API Reference.

## Where a change lands

| You changed | Update |
|---|---|
| A config field | The generated settings reference picks it up from the `Field(description=)`. Mention it on the page that explains the feature only if it changes what a reader does. |
| A CLI option | The API reference picks up the docstring and help text. The installation page that uses the command gets the new step. |
| A tentacle's behaviour | Its usage page, and the shared overview if the change affects the matrix or a table. |
| A platform's setup steps | Its channel page. Keep the platform's own terms for its settings. |
| A gateway spell | `usage/gateway.md`, and `usage/mcp/octomate.md` if the served name or availability changed. |
| The graph | `concepts/reflex.md` and its Mermaid diagram. |
| An extension point | The matching Contributing page and its skeleton. |
| A migration or a table | `concepts/persistence.md` only if the shape in its tree changed. |
| A docstring | Nothing; the reference rebuilds. |

Add the page to `nav:` in `mkdocs.yml` or the strict build fails. The build also
fails on a broken link or anchor. There is no unlisted page and no excluded page.

## Write it true

Every sentence about behaviour is checked against the code before it is written:
the field's description, the command's `--help`, the handler that does the thing.
Where the code and an older note disagree, the code wins and the note is fixed or
deleted. Say what does not work as plainly as what does; a "Not yet" list on an
agent page is more useful than silence.

Say when a command writes data, and which directory it runs in. Use invented names
and `example.com` domains. Never copy a real deployment's hostnames, paths, tokens
or logs into the docs.

Keep prose about choosing and using a feature in the guide, and exact contracts in
the reference. Explanations of behaviour that a developer needs belong in the
docstring, where the reference renders them, not in a paragraph the code cannot
see. Field descriptions belong at the field.

Voice: short sentences, one idea each. Tables for parallel facts, lists for steps,
prose for argument. No emoji in headings, no decorative admonitions; use a note for
a caveat the reader must not miss and a warning for something that writes or
deletes data. Link to a page by relative path so links work in the repository and
on the site. Link to source outside `docs/` with a full GitHub URL.

## The API reference

Each package area has a section in the left sidebar and an overview under
`docs/api/`. Focused pages beneath it group related modules: configuration for
agents, conversation managers, CLI commands, and so on. Move a new topic into
its own page rather than expanding a package overview into a long symbol list.
Keep each module's `:::` directive on exactly one page.

Keep the right-hand TOC available for navigating the current page's modules,
classes and methods. The left sidebar selects the topic; the TOC locates a symbol
within it. Generated symbols retain their signatures, source and linkable anchors:

```markdown
# Threads and conversations

::: octomate.managers.thread
```

Add every page to its sidebar section in `mkdocs.yml`. When moving a module,
update any explicit links to its old page and anchor.

mkdocstrings reads the source statically, so the build needs no server
dependencies and cannot import the application. A Pydantic model renders its
fields from `Field(description=...)`; a class with no docstring renders as a bare
signature, which is the signal to add one. Options are set once in `mkdocs.yml`:
Google-style docstrings, members in source order, private names hidden.

## Build

```sh
uv sync --locked
uv run --no-sync mkdocs build --strict
uv run --no-sync mkdocs serve --livereload --dev-addr 127.0.0.1:8001
```

Pass `--livereload` every time. It is MkDocs' default on paper, but with click 8.2 or
newer the hidden flag resolves to off, so a plain `serve` neither watches files nor
reloads the browser and shows the same build until it is restarted. Port 8000 is
usually Octomate's. A separate environment with only the docs group:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/octomate-docs uv sync --locked --only-group docs
UV_PROJECT_ENVIRONMENT=/tmp/octomate-docs uv run --no-sync mkdocs build --strict
```

Never run `--only-group docs` against a server's runtime environment; sync removes
what is outside the group. The `site/` output is ignored by git.

## Publishing

`.github/workflows/docs.yml` builds strictly on every pull request and, on a push
to `main`, uploads the site and deploys it through GitHub's Pages API, the same
shape as Arcanus's workflow. No `gh-pages` branch, no deploy token. Publication
needs **Settings → Pages → Source: GitHub Actions** set once by an administrator.
The site is `https://kalynnka.github.io/octomate/`.

## The README

The README is the pitch and the map, not a second manual. It says what Octomate
is, shows the shape of a deployment, and links here for everything with steps. A
fact that lives in the README and on a page will drift; keep it on the page.
