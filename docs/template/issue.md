# A unit of work, as an issue

One unit is one reviewable diff, and one Linear issue under its plan's project. The
issue carries what changes and how you will know it worked; the shape it serves lives
in the project body — see [project.md](project.md).

## The issue

```markdown
Title    imperative — "Give ThreadManager the ensure lock"

What changes, and what breaks without it. Enough detail to start from, including the
traps worth naming — a surprising column type, a constraint that will not hold, a call
site that silently depends on the old shape.

**Acceptance**
- [ ] the observable result

**Verification**
- [ ] unit / manual
```

Markdown converts to rich text on the way in, so `- [ ]` arrives as a real checkbox to
tick as the work lands, and a bare `OCTO-12` arrives as a link plus a `related` relation.

## Amending

An issue is a record, not a draft. When a decision overtakes what is written, strike the
outdated sentence through and put the correction under it. Never delete a line, and never
edit one in place — a ticket that only ever says the current answer cannot tell you which
answers were tried.

```markdown
~~It belongs on the config object that owns the roots — a new manager for a pure
config lookup would be an entity without a need.~~

**Superseded 2026-08-04:** the registry is a DB-backed `ProjectManager` seeded from
YAML, mirroring `UserManager`. Decided in the comments.
```

Strike only what stopped being true. A paragraph that is half wrong gets the wrong half
struck and the right half left alone; acceptance criteria that still hold are not touched
at all. A trap that was superseded rather than disproved keeps its hazard — say what
handles it now.

The argument goes in a comment, not the body: one comment per decision, naming what
changed and why. Keep it to a few sentences — the decision, and the one reason it beat
the alternative. A comment that re-explains what the body already carries, or narrates
reasoning the diff makes obvious, costs more to read than it returns. The body carries
what is true and what used to be; the thread carries how it moved.

## Status

`Backlog` → `Todo` → `In Progress` → `In Review` → `Done`

`In Review` means staged and awaiting a read. It is the state the git index used to
carry alone.

## Splitting

When a unit spans layers, split it into sub-issues in reading order: schema and model
first, then managers, then call sites. A unit that cannot be read in one sitting is
two units.

## Dependencies

`Blocked by` goes here, on the unit that actually cannot start — never on the project.
Linear has no project-to-issue relation, and the project is not the thing that is
stuck. Plan-to-plan ordering is a project dependency, set in the Linear UI.
