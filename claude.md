# Repository Guidelines

## Code styles

1. Do not abuse comments, elegant Elegant, straightfoward code is always better then comments. Add comments only necessary. Avoid drawing split lines with comments like this: `# ── Segment data types ───────────────`
2. Always import at top of the file or module, unless there's a circular import issue. If so add comments to clearify the suitation.
3. Explicitly define attributes of classes with proper typehints. Annotated with `ClassVar` if it's a classvar.
4. Don't eagerly abstract code. Create reusable functions or wrappers only if there are at least 3 places that need the same code block. Never create `_xxx` helper methods that are only called once — inline them at the call site.
