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
export function Disclose({ open, style }: { open: boolean; style?: CSSProperties }) {
  return (
    <span className="lt-disclose" data-open={open ? '' : undefined} style={style}>
      ▸
    </span>
  )
}
