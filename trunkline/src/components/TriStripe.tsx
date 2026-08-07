import type { CSSProperties } from 'react'

const segments = (
  <>
    <i style={{ flex: 1, background: 'var(--color-teal)' }} />
    <i style={{ flex: 1, background: 'var(--color-gold)' }} />
    <i style={{ flex: 1, background: 'var(--color-red)' }} />
  </>
)

/** The Lonetrail teal·gold·red stripe, pinned to its parent's bottom edge. */
export function TriStripe({ height = 3 }: { height?: number }) {
  return (
    <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height, display: 'flex' }}>
      {segments}
    </div>
  )
}

/** Inline stripe block (e.g. the 64px rule under a Control page title). */
export function TriStripeInline({ style }: { style?: CSSProperties }) {
  return <div style={{ display: 'flex', height: 3, ...style }}>{segments}</div>
}
