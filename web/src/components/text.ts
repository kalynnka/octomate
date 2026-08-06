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
