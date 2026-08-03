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
a bare `KAL-12` arrives as a link plus an automatic `related` relation.

`## Units` is the checklist before it is a set of issues. Draft it in the body, then
select it and press `Cmd/Ctrl+Shift+O` to promote the lines you agree with into issues.

`## Shipped` grows as units land, naming each one and the commit that carried it. It is
the only tie back to git, so it is worth keeping current.

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
