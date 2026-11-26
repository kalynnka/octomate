import type { CSSProperties, ReactNode } from 'react'

/** Lonetrail vertical fold — grid-rows 1fr→0fr, exactly one child. */
export function Fold({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <div className="lt-fold" data-folded={open ? undefined : ''}>
      <div>{children}</div>
    </div>
  )
}

/** Rotating ▸ disclosure marker. */
export function Disclose({ open, style }: { open?: boolean; style?: CSSProperties }) {
  return (
    <span className="lt-disclose" data-open={open ? '' : undefined} style={style}>
      <svg width="8" height="8" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="m4 2 5 4-5 4Z" /></svg>
    </span>
  )
}
