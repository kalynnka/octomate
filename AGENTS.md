# Guidelines

## Engineering Judgment

1. Apply first-principles reasoning before acting. Treat user instructions as important, but do not assume they are automatically correct. State meaningful assumptions explicitly. If an instruction appears technically unsound, ambiguous, risky, or inconsistent with the repository, raise the concern, verify the assumptions, present the relevant interpretations or tradeoffs, and ask before proceeding when the right path is unclear.
2. Follow Occam's razor: do not add entities, layers, wrappers, helpers, features, configurability, or abstractions without a clear need. Avoid premature abstraction. Introduce reusable functions or wrappers only after the same code path is needed in at least three places. If the implementation is larger than the problem warrants, simplify it.
3. Preserve the existing architecture and local conventions unless there is a concrete reason to change them. Prefer the simplest approach that satisfies the request, and push back when a requested or implied approach adds needless complexity.

## Code Style

1. Write elegant, straightforward code instead of relying on explanatory comments. Add comments only when they clarify non-obvious behavior. Do not use decorative divider comments.
2. Imports should stay at the top of the file or module. If a local import is required to avoid a circular dependency, add a concise comment explaining why.
3. Do not create private-looking `_xxx` helper methods that are called only once. Inline the logic at the call site unless there is a clear reuse or readability benefit.
4. Do not create simple pass-through function wrappers that add no logic, policy, validation, or readability benefit. Call the underlying API directly.
5. Do not overuse the `_` prefix to mark attributes or methods as private. Python does not enforce real private members; use public names unless there is a specific reason to signal internal use.
6. Keep changes surgical. Touch only the code required for the request, match existing style, and do not refactor, reformat, or clean up adjacent code unless it is necessary for the task. Mention unrelated dead code or issues instead of deleting them.
7. Remove imports, variables, functions, or other code made unused by your own changes. Do not remove pre-existing unused code unless asked.
8. Do not add fallback control flow or error handling unless it is explicitly required by the product behavior or caller contract. Prefer fail-fast errors with clear messages over silent retries, alternate execution paths, or best-effort recovery that hides broken assumptions.

## Execution

1. For multi-step work, define brief success criteria before changing code. Map the work to verifiable steps such as reproducing a bug, adding focused tests, implementing the change, and running the relevant checks.
2. Loop until the success criteria are verified or a concrete blocker is found. If verification is not possible, state what could not be checked and why.
3. Every changed line should trace directly to the user's request, the agreed success criteria, or cleanup made necessary by the change.

## Typing

1. Class attributes should be explicitly defined with proper type hints. Use `ClassVar` for class variables.
2. Do not leave Python type hint warnings or type checker errors. Always satisfy the configured type checker.
3. Do not use `typing.Any` or `object` in type hints. Use precise concrete types, `TypeVar` generics, discriminated unions, `TypedDict`, or narrow `Protocol` contracts. Validate external payloads at clear boundaries before passing them deeper.
4. Prefer precise collection types in annotations when the runtime shape is known (`list[T]` or `tuple[...]`) instead of broad abstractions like `Sequence[T]`; this also keeps Pydantic validation cheaper and clearer.
5. Prefer `TypedDict` for simple structured tool arguments and request payloads when a full model adds no behavior. Use `TypedDict` plus `**payload` unpacking to pass structured payloads through typed call sites instead of building ad hoc dict wrappers.
6. Do not duplicate the same payload shape as both a model and a `TypedDict` without a concrete reason.
7. Do not use `cast`, `type: ignore`, or pyright suppressions merely to satisfy the type checker. Fix the annotation, model the optional/variant shape honestly, or move validation to the correct boundary. Use casts only at true dynamic boundaries where the runtime type has already been established and cannot be expressed otherwise.

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

## Architecture

1. Web APIs are FastAPI routers owned by the single project-level Octomate instance; do not force web routes into the channel tentacle lifecycle.
2. Channel tentacles are for long-lived IM platform connections and translation only. They call Octomate directly for dispatch; do not reintroduce nerve or dispatcher-style object streams between channels and agents.
3. Use the concrete tentacle base class hierarchy for Octomate-managed agents and channels. Do not introduce extra Protocol wrappers for Octomate, tentacle, or channel lifecycle contracts.
