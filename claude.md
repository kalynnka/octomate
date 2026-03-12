# Repository Guidelines

## Code styles

1. Do not abuse comments, elegant Elegant, straightfoward code is always better then comments. Add comments only necessary. Avoid drawing split lines with comments like this: `# ── Segment data types ───────────────`
2. Always import at top of the file or module, unless there's a circular import issue. If so add comments to clearify the suitation.
3. Explicitly define attributes of classes with proper typehints. Annotated with `ClassVar` if it's a classvar.
4. Don't eagarly abstract codes, create reusable or wrappers only if there's a 3rd place need the same code block.
