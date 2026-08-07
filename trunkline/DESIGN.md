# Design — Trunkline console

The visual world is **Lonetrail**, the design system of the user's Claude
Design project ("Rhine Lab field-report dossier": cream archival paper, one
burnt-orange accent, hard corners, tiny mono labels, a recurring
teal·gold·red tri-stripe). It is vendored, not paraphrased: the token files in
`src/styles/` are the design system's own CSS, and this file records how the
console uses them. Source comp: `Trunkline Console.dc.html` in the user's
design project.

## Files

| File | Role |
| --- | --- |
| `src/styles/lonetrail.css` | Colors (light + `[data-theme="dark"]`), typography, shape, elevation — verbatim from the DS (`colors_and_type.css`), font URLs pointed at `/fonts/` |
| `src/styles/motion.css` | Motion tokens, keyframes, `lt-*` utilities — verbatim from the DS |
| `src/styles/decorators.css` | Grid-paper + dot-band decorators — verbatim from the DS |
| `src/styles/console.css` | Console-screen layer: `--trk-*` tokens, rail fold/drag classes, draft-grid texture, scrollbars, hover utility classes |
| `public/fonts/` | Novecento Sans Wide (otf ×3), Plus Jakarta Sans (variable woff2), Fira Code + Noto Serif SC latin subsets (vendored woff2); only Noto's CJK subsets remain on the Google Fonts CDN |

## Identity in one look

Cream paper (`#EDE6D6`) under warm near-black ink (`#1A1814`); everything
squared off (radius 0; only status dots are round and small chips 2px); one
saturated accent, burnt orange `#D4621A`, spent on ids, active states, and
primary actions; the info **blue ramp** (`--info*`) is the second functional
color — links, quotes, VS Code affordances — never success/warning/CTA; the
teal·gold·red tri-stripe signs panel bottoms and section rules; labels are
Fira Code, bold, uppercase, tracked `.1em–.22em`, sizes 6.5–10px; prose is
Noto Serif SC 12.5–14.5px at 1.65–1.75 line-height. Dark theme is a warm
darkroom (`#171410` page, brightened accents), toggled by `data-theme="dark"`
on `<html>`; `auto` follows `prefers-color-scheme` (cycled in JS, persisted
as `trk-theme`).

## Type roles

- **Display** (`--font-display`, Novecento): screen titles, feeler titles,
  brand wordmark, blank-state headings — always uppercase, 600/900.
- **Mono** (`--font-mono`, Fira Code): every label, id, chip, path, metric,
  timestamp, code line. The console's data voice.
- **Serif** (`Noto Serif SC`): chat prose, thinking (italic), dossier
  paragraphs, comments (italic), timeline "reason" lines (italic).
- **Sans** (`--font-sans`): almost unused directly — the shell default only.

## Console tokens (`console.css`)

- `--trk-bracket` — the 2px corner-bracket + composer frame color
  (42% ink; dark: 38% cream).
- `--trk-vline` — hairline separators inside bars (14% ink).
- `--surface-raised` — dropdown/menu surface (`#F2ECDD`; dark `--ce-raised`).
- `--trk-wash` / `--trk-wash-strong` — 3% / 6% ink washes: row hovers and
  quiet fills / code-block and table-header fills. Both ride `--color-ink`,
  so they flip with the theme on their own.
- `--trk-on-fill` — text on saturated fills (accent/terra/teal chips and
  buttons): cream `#F6F1E5` in both themes, because the fill never flips.
- `trkBlink` (composer caret), `trkPulse` (working/pending pulse),
  `trkFlash` (timeline-jump highlight ring) — the only console-specific
  keyframes; everything else uses the DS `lt-*` set.

## Label roles (`components/text.ts`)

Named `label()` presets extracted from the comp's recurring voices; color
stays at the call site. Sizes/trackings outside these roles remain literal
`label(size, tracking)` calls — the comp deliberately drifts around its own
grid, and near-misses are not snapped.

| Role | Type | Voice |
| --- | --- | --- |
| `cardKind` | 10px / .2em | ledger card kind — "Thinking", "Tool", "Plan" (`--fg-1`) |
| `metaLine` | 9px / .2em | sender · timestamp, run ids, durations (`--fg-3`) |
| `fieldLabel` | 8px / .2em | field name over a value — "Project", "Branch" (`--fg-2`) |
| `sectionLabel` | 8px / .16em | section headers in panels and settings (`--fg-3`) |
| `statusNote` | 8px / .18em | status/hint lines — "● Active", scroll hints |
| `microSection` | 7.5px / .16em | small stat-block headers |
| `microMeta` | 7.5px / .14em | meta rows inside cards and control lists |
| `chipLabel` | 7px / .12em | tiny chips in the timeline and control rail |

## Layout

Fixed-height shell (`100vh`), five columns left→right, plus a 26px status
bar: threads sidebar (26px letter-rail + tree, `clamp(200px,21vw,272px)`,
drag 170–420px) · Control rail (folded by default) · review dossier panel
(folded; `clamp(440px,40vw,720px)`, drag 390–940px) · chat main (flex) ·
timeline (`clamp(240px,25%,330px)`, drag 200–480px, left-edge handle).
Chat card width steps with open panels: 64/78/88/92% (`--trk-card-max`).
Every rail folds by animating `width` (`.trk-rail[data-folded]`), drags via a
6px edge handle, and signs off with the tri-stripe.

## Recurring patterns

- **Corner brackets** frame tool results and file cards (`Brackets` in
  `cards.tsx`) — the dossier's "clipped document" mark; summon tools instead
  take a full terra border.
- **Feelers**: gold left edge + pulsing gold dot while waiting; resolve to a
  one-line record (sage for approve/answer, ghost for dismiss).
- **Session rules**: accent-dot hairline for entry sessions; double terra
  rules for a summon handover; dashed teal rules for teleport notices.
- **End-of-turn**: centered hairline with an uppercase usage summary.
- **Terminal composer**: `>_ kalynnka@trunkline:route` prompt line over the
  input, `Directive` toolbar, agent·model·effort selector, session bar with
  live clock, usage, and the 56px hatched context meter.
- **Menus** rise 6px on open (`lt-menu`), sit on `--surface-raised` with the
  card shadow.

## Motion rules (delegated details, decided here)

Lonetrail's contract: entries rise 8px once (380ms `--ease-out`, staggered
70ms via `--i`), folds are 240ms grid-template transitions, hovers are 150ms
color swaps, and the only loops are functional status — `trkPulse` on
anything waiting/running, `lt-dots` while an agent works, `lt-caret` while a
reply streams, skeleton rows for a pending session. The console adds exactly
one flourish inside that contract: `trkFlash`, a 1.4s accent ring on the
ledger row a timeline jump lands on. All motion honors
`prefers-reduced-motion`.

## Don'ts

No rounded cards, no gradients-as-decoration, no glass/backdrop-blur, no
emoji-as-icons (Lucide-style 2px outlines or typographic glyphs `▸ ⌗ ⇄ ↳`
only), no ink-on-saturated-fill (saturated fills carry `#F6F1E5`/white text),
no tri-stripe as UI semantics (it is a signature, not a status), no
decorative loops.
