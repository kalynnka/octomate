import type { CSSProperties } from 'react'

/** Fira Code run — the console's data voice. */
export const mono = (fontSize: number, fontWeight = 400): CSSProperties => ({
  fontFamily: 'var(--font-mono)',
  fontSize,
  fontWeight,
})

/** Mono field-report label: bold, uppercase, tracked. */
export const label = (fontSize: number, letterSpacing = '.2em'): CSSProperties => ({
  ...mono(fontSize, 700),
  letterSpacing,
  textTransform: 'uppercase',
})

/** Novecento display run. */
export const display = (fontSize: number, fontWeight = 900): CSSProperties => ({
  fontFamily: 'var(--font-display)',
  fontWeight,
  fontSize,
})

/* Named label roles, extracted from the design comp (`Trunkline Console.dc.html`).
   Each names a recurring voice; color stays at the call site (the customary ink
   is noted). Off-role sizes/trackings remain literal `label(...)` calls. */

/** Ledger card kind header — "Thinking", "Tool", "Plan". Customarily --fg-1. */
export const cardKind = label(10)
/** Ledger meta line — sender · timestamp, run ids, durations. Customarily --fg-3. */
export const metaLine = label(9)
/** Field name over a value — "Project", "Branch", chip fields. Customarily --fg-2. */
export const fieldLabel = label(8)
/** Section header inside panels and settings pages. Customarily --fg-3. */
export const sectionLabel = label(8, '.16em')
/** Status or hint note — "● Active", "↑ scroll — earlier messages". */
export const statusNote = label(8, '.18em')
/** Small stat-block header — "Gateway verbs · 7d". */
export const microSection = label(7.5, '.16em')
/** Meta row inside cards and control lists. */
export const microMeta = label(7.5, '.14em')
/** Tiny chip — timeline and control chips. */
export const chipLabel = label(7, '.12em')

/** Noto Serif prose — chat bodies and dossier paragraphs. */
export const serif = (fontSize: number): CSSProperties => ({
  fontFamily: "'Noto Serif SC', serif",
  fontSize,
})

export const ellipsis: CSSProperties = {
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
}
