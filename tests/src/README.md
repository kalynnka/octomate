# Test Source Assets

- `events/` stores raw Pydantic AI stream captures as JSONL.
- `images/` stores source images used by captures and trigger replays.

Drop real images in `images/` (any `*.png` / `*.jpg` / `*.jpeg` / `*.gif` /
`*.webp`) and the `@trigger` live-replay tests will upload one as the showcase's
image segment. With no image present the tests fall back to a generated 1x1 PNG.
