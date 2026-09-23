# Plans and issues

Work on Octomate is written up before it is built. A plan is a Linear project; a
unit of work is one issue under it, one reviewable diff. The templates below are
what those bodies look like.

## A plan, as a project

The project body carries the whole plan, the argument as well as the summary, so
there is one place to read it and one place to change it.

```markdown
## TL;DR
Two to four sentences. What cannot be done today, and what this makes possible.

## Outcome
What is true when this is done. Present tense, observable.

## Not this
The three or four things a reader would assume are included, and are not.

## What already exists
What is on main today, what was measured rather than reasoned, what must not be
rebuilt. Say when something is not verified, and how to check it.

## Requirements
The properties the result must have, numbered so a unit can cite one.

## Direction
The shape proposed, and the alternatives weighed. Record rejected directions with
the reason; a later reader needs to know the road was walked.

## Units
- [ ] imperative, one reviewable diff each

## Risks
Only when a unit can break something live.

## Open questions
The undecided. Delete each as it is settled.

## Acceptance
- [ ] the checks that settle the whole plan

## Shipped
- unit — sha
```

Delete what does not apply. `Units` is the checklist before it is a set of issues;
draft it in the body, then promote the lines you agree with. `Shipped` is the only
tie back to git, so keep it current. Name the project after the outcome, not the
mechanism.

## A unit, as an issue

```markdown
Title: imperative, "Give ThreadManager the ensure lock"

What changes, and what breaks without it. Enough to start from, including the
traps worth naming.

**Acceptance**
- [ ] the observable result

**Verification**
- [ ] unit / manual
```

Status runs `Backlog` to `Todo` to `In Progress` to `In Review` to `Done`, where
`In Review` means staged and awaiting a read. A unit that spans layers is split in
reading order: schema and model, then managers, then call sites. `Blocked by` goes
on the unit that cannot start, never on the project.

## Amending

An issue or a plan is a record, not a draft. When a decision overtakes what is
written, strike the outdated sentence through and put the correction under it,
dated. Never delete a line and never edit one in place: a body that only ever says
the current answer cannot tell you which answers were tried. The argument goes in a
comment, one per decision, a few sentences. A rejected direction that later wins is
struck where it was rejected and the adoption noted there. Open questions are the
exception: they are deleted as they settle and the answer lands in Direction.
