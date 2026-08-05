# A plan, as a project

A plan is a Linear project. The project body carries the whole plan — the argument as
well as the summary — so there is one place to read it and one place to change it.

Units of work are separate issues — see [issue.md](issue.md).

## The body

```markdown
## TL;DR

Two to four sentences. What cannot be done today, and what this makes possible.
Someone who reads nothing else should be able to stop here.

## Outcome

What is true when this is done. Present tense, observable.

## Not this

The three or four things a reader would reasonably assume are included, and are not.

## What already exists

The grounding: what is on `main` today, what was measured rather than reasoned, what
must not be rebuilt. Say when something is *not* verified, and say how to check it.

## Requirements

The properties the result must have, numbered so a unit can cite one.

## Direction

The shape being proposed, and the alternatives weighed against it. Record rejected
directions with the reason — a later reader needs to know the road was walked.

## Units

- [ ] <imperative — one reviewable diff>
- [ ] <the next one>

## Risks

Only when a unit can break something already live.

## Open questions

The undecided. Delete each as it is settled.

## Acceptance

- [ ] the checks that settle the whole plan, concrete enough to disagree with

## Shipped

- <unit> — `<sha>`
```

Delete what does not apply. A raw note may be nothing but a TL;DR and open questions
until it is shaped.

Markdown converts to rich text on the way in, so `- [ ]` arrives as a real checkbox and
a bare `OCTO-12` arrives as a link plus an automatic `related` relation.

`## Units` is the checklist before it is a set of issues. Draft it in the body, then
select it and press `Cmd/Ctrl+Shift+O` to promote the lines you agree with into issues.

`## Shipped` grows as units land, naming each one and the commit that carried it. It is
the only tie back to git, so it is worth keeping current.

## Amending

Same rule as a unit's — strike the outdated line through, add the correction beneath, put
the argument in a comment. See [issue.md](issue.md). A plan is the body a unit cites, so
an amendment that is not made here is one every later unit reads as still true.

Three cases are specific to a plan body:

- A rejected direction that later wins is struck **where it was rejected** and the
  adoption noted there. `## Direction` exists so a reader knows the road was walked; a
  rejection quietly deleted once it turns out to be right is the one loss the section
  cannot afford.
- A rejection that is half overtaken keeps its standing half. "No table and no FK" that
  becomes "a table, still no FK" is struck and restated, not deleted.
- `## Open questions` is the exception: those are deleted as they settle, and the answer
  lands in `## Direction`. They are the plan's scratch space, not its record.

## The fields

| Field | |
|---|---|
| Name | the outcome, not the mechanism — "One Gateway for every agent" |
| Summary | the TL;DR's first sentence, verbatim (≤255 chars) |
| Status | `Backlog` proposed · `In Progress` · `Completed` · `Canceled` |
| Initiative | the repo |
| Label | one, by subsystem |
| Lead | @luhui |

Off by default: target date, milestones, priority. Add a milestone only when a plan has
genuine sequential phases, which most do not.
