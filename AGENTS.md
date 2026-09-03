# Guidelines

## Engineering Judgment

1. Apply first-principles reasoning before acting. Treat user instructions as important, but do not assume they are automatically correct. State meaningful assumptions explicitly. If an instruction appears technically unsound, ambiguous, risky, or inconsistent with the repository, raise the concern, verify the assumptions, present the relevant interpretations or tradeoffs, and ask before proceeding when the right path is unclear.
2. Follow Occam's razor: do not add entities, layers, wrappers, helpers, features, configurability, or abstractions without a clear need. Avoid premature abstraction. Introduce reusable functions or wrappers only after the same code path is needed in at least three places. If the implementation is larger than the problem warrants, simplify it.
3. Preserve the existing architecture and local conventions unless there is a concrete reason to change them. Prefer the simplest approach that satisfies the request, and push back when a requested or implied approach adds needless complexity.

## Code Style

1. Write elegant, straightforward code instead of relying on explanatory comments. Add comments only when they clarify non-obvious behavior. Do not use decorative divider comments.
2. Imports should stay at the top of the file or module. If a local import is required to avoid a circular dependency, add a concise comment explaining why.
3. Do not abuse the `_` prefix to mark attributes or methods as private. Python does not enforce real private members; use public names unless there is a specific reason to signal internal use.
4. Keep changes surgical. Touch only the code required for the request, match existing style, and do not refactor, reformat, or clean up adjacent code unless it is necessary for the task. Mention unrelated dead code or issues instead of deleting them.
5. Remove imports, variables, functions, or other code made unused by your own changes. Do not remove pre-existing unused code unless asked.
6. Do not add fallback control flow or error handling unless it is explicitly required by the product behavior or caller contract. Prefer fail-fast errors with clear messages over silent retries, alternate execution paths, or best-effort recovery that hides broken assumptions.
7. When an attribute needs documentation, put it at the definition: a Pydantic `Field(description=...)` for model fields, and a brief comment on the field for dataclass, `TypedDict`, or plain-class attributes (which have no description slot). Prefer this over a free-floating comment above the attribute.
8. The tree is `ruff format`ed. Run `uv run ruff format` and `uv run ruff check` on the files your change touches, and pass paths explicitly — never format the whole tree, which buries the change under unrelated reflow.
9. Never put a ticket identifier in code, a docstring, a comment, or a document. Say the reason itself: a reader of the code has no tracker in front of them, and the identifier is what the commit that closed the ticket records.

### Helpers

1. Before adding a helper, search the codebase for existing functions, methods, classes, or manager APIs that already own the behavior; reuse or adjust the existing owner when that is clearer than creating a duplicate.
2. Add a helper only when it names real policy, removes meaningful complexity, or is reused enough to earn its existence. Inline one-off formatting, branching, and simple call sequences at the call site unless extracting them makes the caller substantially easier to understand.
3. Do not create private-looking `_xxx` helper methods that are called only once. Inline the logic at the call site unless there is a clear reuse or readability benefit.
4. Do not create simple pass-through function wrappers that add no logic, policy, validation, or readability benefit. Call the underlying API directly.
5. Place helpers with the object or module that owns the data and dependencies they use. Prefer a method on the relevant manager, dependency object, schema/state object, or cohesive module over a detached utility function. When several helpers are related, introduce or reuse a small owner module/class instead of scattering script-like helper functions.
6. Prefer public helper names unless there is a concrete reason to signal internal use. A private-looking `_xxx` helper must still have a clear owner and enough reuse or complexity to justify existing.

## Execution

1. For multi-step work, define brief success criteria before changing code. Map the work to verifiable steps such as reproducing a bug, adding focused tests, implementing the change, and running the relevant checks.
2. Loop until the success criteria are verified or a concrete blocker is found. If verification is not possible, state what could not be checked and why.
3. Every changed line should trace directly to the user's request, the agreed success criteria, or cleanup made necessary by the change.
4. Committing, pushing, and opening a pull request each require the user's approval **in their most recent message**. Approval is scoped to that turn and expires with it: an earlier "commit and merge" was the instruction for the work that existed then, and says nothing about the work that came after. Having committed once, or many times, in a session grants nothing for the next piece of work. Treat any instruction from an earlier message as outdated for this purpose.
5. Absent that approval, finish the work, run the checks, report what changed, and stop. A request to do work is not a request to publish it, and creating a branch first does not make an unrequested commit acceptable — branching is what to do *when* committing, not a licence to commit.
6. When undoing a commit for review, preserve its changes in the working tree unless the user explicitly asks to discard them.
7. A branch is named `<kind>/<slug>`, taking `kind` from the same set the commit subjects use — `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`. Never put a person in a branch name: git already records the author, and the name should say what the work is. Include the issue identifier when there is one, so Linear links the branch to it — `feat/octo-29-project-registry`. A tool that suggests a branch name, Linear included, does not override this.

## Typing

1. Class attributes should be explicitly defined with proper type hints. Use `ClassVar` for class variables.
2. `ruff` is the source of truth: `uv run ruff check` and `uv run ruff format --check` must both pass, and its configured rule set in `pyproject.toml` is what "clean" means.
3. Pylance is what the human actually reads, so a clean editor matters too — leave no pyright squiggle behind. It runs at `typeCheckingMode: "basic"` (`.vscode/settings.json`), which is what "clean" means for it; the CLI defaults to the stricter `standard`, so measure in `basic` before claiming parity. Satisfy it by fixing the type wherever fixing it is honest. Suppress with `# pyright: ignore[<rule>]` plus a comment naming which of exactly three cases applies: it contradicts ruff, it is plainly wrong about the runtime, or the rejected call *is* the input under test (a test asserting that a bad value is refused). Anything else is a real finding — fix it. A bare `# type: ignore`, or one with no reason, is not acceptable.
4. Ruff and pyright do disagree, and neither wins by default — the fix that satisfies both is almost always available and is the one to find. Where it genuinely is not, keep the code the type checker can verify and suppress the ruff rule, because a lint preference costs less than a checked type: C416's `dict(x)` loses the key-widening a comprehension does, and RUF019's `.get()` loses the `in` narrowing a `TypedDict` needs. Both are real conflicts already suppressed in-tree; add to that list rather than trading a checked type for a tidier line.
5. Do not use `typing.Any` or `object` in type hints. Use precise concrete types, `TypeVar` generics, discriminated unions, `TypedDict`, or narrow `Protocol` contracts. Validate external payloads at clear boundaries before passing them deeper.
6. Prefer precise collection types in annotations when the runtime shape is known (`list[T]` or `tuple[...]`) instead of broad abstractions like `Sequence[T]`; this also keeps Pydantic validation cheaper and clearer.
7. Prefer `TypedDict` for simple structured tool arguments and request payloads when a full model adds no behavior. Use `TypedDict` plus `**payload` unpacking to pass structured payloads through typed call sites instead of building ad hoc dict wrappers.
8. Do not duplicate the same payload shape as both a model and a `TypedDict` without a concrete reason.
9. Do not use `cast`, `type: ignore`, or pyright suppressions merely to satisfy the type checker — silencing a true positive is the failure this guards against, and it is not what rule 3's three cases license. Fix the annotation, model the optional/variant shape honestly, or move validation to the correct boundary. Use casts only at true dynamic boundaries where the runtime type has already been established and cannot be expressed otherwise.
10. A rule that has to bend bends at the line that bends it. Suppress with `# noqa: <CODE>` on that line, and give the reason in a comment above it — or once at the first occurrence in a file when the same suppression repeats for one library contract. Never add to `pyproject.toml`'s `per-file-ignores`: a file-wide ignore silently exempts code written later that never earned it, and nothing detects it once it stops applying, whereas RUF100 flags a `noqa` the moment it goes stale. A suppression with no code, or with no reason, is not acceptable either way.

## Data Modeling

1. Model real domain concepts directly instead of hiding them in loose metadata dictionaries. If two cases have different fields, statuses, or behavior, represent them as separate typed variants.
2. Prefer discriminated unions plus `TypeAdapter` at dynamic boundaries. Validate external or serialized payloads once into typed variants, then pass those typed objects through the rest of the code.
3. When prepared data is ultimately passed to a Pydantic model, schema, or `TypeAdapter`, let that final Pydantic boundary perform validation. Do not eagerly instantiate or validate each nested item first unless the intermediate code must inspect typed fields, branch on the validated shape, or produce a deliberately earlier error.
4. Keep batch/action ownership clear: batches group actions; each action represents exactly one user-facing approval or one user-facing question. Do not wrap multiple logical actions inside one action's metadata.
5. Use polymorphic ORM/schema mappings when persisted variants share a table but have distinct types. The ORM inheritance, schema classes, and status types should line up with the domain variants.
6. Preserve platform callback state as typed action data where practical. Cards and blocks may serialize ids and fields, but callback handlers should rehydrate typed actions before applying business logic.

## Persistence

1. Always leverage Arcanus for database persistence. Application code should use Pydantic schema/transmuter types and Arcanus session APIs; do not import, query, update, or delete ORM models directly unless you are defining the schema/model mapping itself.
2. Prefer ORM-style persistence through loaded transmuter objects. When an object is already in the session, mutate its typed attributes and commit instead of issuing manual SQL `update()` calls or copying the transmuter to mirror the change.
3. Foreign keys are enforced (`PRAGMA foreign_keys=ON`, set per connection in `octomate/database.py`), and tests run under the same constraint via `database.create_engine`. A row therefore needs its parents to exist: build them, or use `tests.support.managers.a_thread`, rather than minting a bare `uuid7()` for a parent id.
4. Every migration is produced by `uv run alembic revision --autogenerate -m "..."`, always, and then adjusted. Never hand-write one. Point `OCTOMATE_DB_URL` at a copy of the database for the run, as with any other migration work. The generated file is what keeps a column's type, constraints and `comment=` identical to the ORM definition that produced them — restating any of that by hand creates a second source of truth that drifts silently, and the drift only shows up as a column whose comment describes something it no longer is.
5. Adjust the generated file afterwards: write the docstring that says why, add a data backfill, drop an operation autogenerate inferred but the change does not want. What must not be typed by hand is the schema operations themselves. If autogenerate produces nothing, the ORM change is missing — fix the model rather than writing the operation.
6. Attribute documentation lives on the ORM column as `comment=`, and reaches the migration by being generated from it. A migration that carries a comment string the model does not have is the drift this rule exists to prevent.

## Observability

1. When investigating with Logfire (the `logfire` MCP tools — trace/SQL queries, dashboards, alerts), delegate the Logfire work to a sub-agent running a cheaper model or lower reasoning effort — Sonnet under Claude Code, a lower effort setting under Codex — instead of spending the main model's budget on it. Hand the sub-agent the specific question (trace id, time range, what to find) and keep only its conclusion.

## Architecture

1. Web APIs are FastAPI routers owned by the single project-level Octomate instance; do not force web routes into the channel tentacle lifecycle.
2. Channel tentacles are for long-lived IM platform connections and translation only. They call Octomate directly for dispatch; do not reintroduce nerve or dispatcher-style object streams between channels and agents.
3. Use the concrete tentacle base class hierarchy for Octomate-managed agents and channels. Do not introduce extra Protocol wrappers for Octomate, tentacle, or channel lifecycle contracts.
