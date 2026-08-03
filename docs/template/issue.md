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
tick as the work lands, and a bare `KAL-12` arrives as a link plus a `related` relation.

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
